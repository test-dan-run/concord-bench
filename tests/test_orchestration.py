"""Jobs and the Model Catalog's leases (platform/catalog/README.md), against aicore's FakeCatalog and a stub
whose lease queues before it is granted: holder, release, `waiting` with queue position, blocked states,
preferring jobs whose models are running, and stage dependencies."""

from __future__ import annotations

import pytest
from aicore.catalog import FakeCatalog

from concord_bench.core.models import Job, Run
from concord_bench.platform import catalog
from concord_bench.runs import jobs
from conftest import catalog_model


@pytest.fixture
def cat():
    fake = FakeCatalog([catalog_model("a", ["vad"]), catalog_model("big", ["asr"], state="stopped"),
                        catalog_model("small", ["diar"], state="running")])
    catalog.use(fake)
    return fake


def _job(session, monkeypatch, handler, models=("a",), after=None) -> int:
    monkeypatch.setitem(jobs.HANDLERS, "fake", handler)
    job = jobs.enqueue(session, "fake", {}, list(models), after)
    session.commit()
    return job.id


def _status(session, job_id: int) -> Job:
    session.expire_all()
    return session.get(Job, job_id)


def test_job_leases_its_models_and_releases_on_success(session, cat, monkeypatch):
    seen = {}

    def handler(session, job, ctx):
        seen["leases"] = [x for x in cat.leases() if x["holder"] == jobs.holder_for(job.id)]
        return {"ok": True}

    job_id = _job(session, monkeypatch, handler)
    jobs.run_pending()
    holder = f"bench:job-{job_id}"
    assert ("lease", (holder, ("a",))) in cat.calls
    assert [x["state"] for x in seen["leases"]] == ["active"]  # held while the job ran...
    assert cat.leases() == [] and cat.calls[-1][0] == "release"  # ...and released at the end
    assert _status(session, job_id).status == "done"


def test_job_releases_on_failure(session, cat, monkeypatch):
    def handler(session, job, ctx):
        raise RuntimeError("model fell over")

    job_id = _job(session, monkeypatch, handler)
    jobs.run_pending()
    job = _status(session, job_id)
    assert job.status == "failed" and "fell over" in job.error
    assert cat.leases() == []


def test_jobs_without_models_take_no_lease(session, cat, monkeypatch):
    job_id = _job(session, monkeypatch, lambda *a: {}, models=())
    jobs.run_pending()
    assert _status(session, job_id).status == "done" and not [c for c in cat.calls if c[0] == "lease"]


@pytest.mark.parametrize("record, reason", [
    (None, "ghost is not in the Model Catalog"),
    (catalog_model("ghost", ["asr"], status="stale"), "not verified for its current build yet (capabilities stale)"),
    ({**catalog_model("ghost", ["asr"]), "capabilities": None}, "has not been verified by the Model Catalog"),
    ({**catalog_model("ghost", ["asr"], status="failed"),
      "capabilities": {"status": "failed", "report": {"lines": [{"name": "route", "status": "fail",
                                                                 "reason": "no /v1/audio/transcriptions"}]}}},
     "failed verification in the Model Catalog (route: no /v1/audio/transcriptions)"),
    ({**catalog_model("ghost", ["asr"]), "registered": False}, "not registered"),
])
def test_unknown_or_unverified_model_blocks(session, cat, monkeypatch, record, reason):
    if record is not None:
        cat.add_model(record)
    job_id = _job(session, monkeypatch, lambda *a: pytest.fail("must not run"), models=("ghost",))
    jobs.run_pending()
    job = _status(session, job_id)
    assert job.status == "blocked" and reason in job.error
    assert any("blocked:" in line and reason in line for line in jobs.read_log(job_id))
    assert not [c for c in cat.calls if c[0] == "lease"]


def test_catalog_down_blocks(session, monkeypatch):
    class Down(FakeCatalog):
        def models(self, *a, **k):
            import httpx

            raise httpx.ConnectError("connection refused")

    catalog.use(Down())
    job_id = _job(session, monkeypatch, lambda *a: pytest.fail("must not run"))
    jobs.run_pending()
    job = _status(session, job_id)
    assert job.status == "blocked" and "not reachable" in job.error


class QueuedCatalog(FakeCatalog):
    """Leases queue at position 2 until `grant()` (or fail with `fail()`)."""

    def __init__(self, models):
        super().__init__(models)
        self.outcome: str | None = None
        self.polls = 0

    def request_lease(self, holder, models, ttl_s=90):
        d = super().request_lease(holder, models, ttl_s)
        self._leases[d["id"]].update(state="queued", position=2,
                                     waiting_for="a does not fit next to qwen3-32b-awq (leased by label:job-3)")
        return dict(self._leases[d["id"]])

    def get_lease(self, lease_id):
        self.polls += 1
        lease = self._leases[lease_id]
        if self.outcome == "active":
            lease.update(state="active", position=None, waiting_for=None)
        elif self.outcome == "failed":
            lease.update(state="failed", error="a failed to start: CUDA out of memory")
        return dict(lease)

    def grant(self):
        self.outcome = "active"

    def fail(self):
        self.outcome = "failed"


def test_queued_lease_waits_for_compute_then_runs(session, monkeypatch):
    cat = QueuedCatalog([catalog_model("a", ["vad"])])
    catalog.use(cat)
    ran: list[int] = []
    monkeypatch.setitem(jobs.HANDLERS, "free", lambda s, job, ctx: ran.append(job.id) or {})
    model_job = _job(session, monkeypatch, lambda s, job, ctx: ran.append(job.id) or {})
    other_model_job = _job(session, monkeypatch, lambda s, job, ctx: ran.append(job.id) or {})
    free_job = jobs.enqueue(session, "free", {}).id
    session.commit()

    assert jobs.run_pending(max_jobs=1) == 1
    job = _status(session, model_job)
    assert job.status == "waiting" and ran == []
    assert job.lease["position"] == 2 and "label:job-3" in job.lease["waiting_for"]
    assert job.message.startswith("waiting for compute #2: a does not fit")
    # while one lease waits, only jobs that need no model run
    assert jobs.run_pending(max_jobs=1) == 1 and ran == [free_job]
    assert jobs.run_pending(max_jobs=1) == 0 and _status(session, other_model_job).status == "queued"
    cat.grant()
    jobs.run_pending(poll_s=0.01)
    assert ran == [free_job, model_job, other_model_job]
    assert _status(session, model_job).status == "done" and _status(session, model_job).lease is None
    assert cat.leases() == []  # both released


def test_lease_that_fails_while_queued_blocks_the_job(session, monkeypatch):
    cat = QueuedCatalog([catalog_model("a", ["vad"])])
    catalog.use(cat)
    job_id = _job(session, monkeypatch, lambda *a: pytest.fail("must not run"))
    jobs.run_pending(max_jobs=1)
    assert _status(session, job_id).status == "waiting"
    cat.fail()
    jobs.run_pending(poll_s=0.01)
    job = _status(session, job_id)
    assert job.status == "blocked" and "CUDA out of memory" in job.error and jobs._waiting == {}


def test_refused_lease_blocks(session, monkeypatch):
    class Refusing(FakeCatalog):
        def request_lease(self, holder, models, ttl_s=90):
            d = super().request_lease(holder, models, ttl_s)
            self._leases[d["id"]].update(state="failed", error="a needs 40 GB; the host has 32 GB")
            return dict(self._leases[d["id"]])

    catalog.use(Refusing([catalog_model("a", ["vad"])]))
    job_id = _job(session, monkeypatch, lambda *a: pytest.fail("must not run"))
    jobs.run_pending()
    job = _status(session, job_id)
    assert job.status == "blocked" and "the host has 32 GB" in job.error


def test_waiting_run_job_mirrors_on_its_run(session, monkeypatch):
    from concord_bench.core.models import Dataset

    cat = QueuedCatalog([catalog_model("a", ["vad"])])
    catalog.use(cat)
    ds = Dataset(name="d", status="ready")
    session.add(ds)
    session.flush()
    run = Run(task="seg", dataset_id=ds.id, status="queued")
    session.add(run)
    session.flush()
    jobs.enqueue(session, "run", {"run_id": run.id}, ["a"])
    session.commit()
    jobs.run_pending(max_jobs=1)
    session.expire_all()
    assert session.get(Run, run.id).status == "waiting"
    assert jobs.recover(session) == 1  # a restarted worker requeues it (the old lease expires on its own)
    session.commit()
    assert session.query(Job).one().status == "queued"


def test_claim_prefers_jobs_whose_models_are_running(session, cat):
    j_big = jobs.enqueue(session, "fake", {}, ["big"])
    j_small = jobs.enqueue(session, "fake", {}, ["small"])
    session.commit()
    job, lease = jobs.claim_next(session)
    assert job.id == j_small.id and lease is None  # small is running: no load
    job, _ = jobs.claim_next(session)
    assert job.id == j_big.id


def test_later_stages_wait_and_block_with_their_earlier_stage(session, cat, monkeypatch):
    calls = {"n": 0}

    def flaky(session, job, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("seg stage fell over")
        return {}

    first = _job(session, monkeypatch, flaky)
    monkeypatch.setitem(jobs.HANDLERS, "assemble", lambda *a: {})
    second = jobs.enqueue(session, "assemble", {}, [], [first]).id
    session.commit()
    job, _ = jobs.claim_next(session)
    assert job.id == first  # the second waits for it, although it needs no model
    jobs.run_job(job.id)
    session.expire_all()
    assert jobs.claim_next(session) is None
    blocked = _status(session, second)
    assert blocked.status == "blocked" and f"needs job #{first}" in blocked.error
    jobs.retry(session, _status(session, first))  # retrying the stage requeues what it blocked
    session.commit()
    assert _status(session, second).status == "queued"
    jobs.run_pending()
    assert _status(session, first).status == _status(session, second).status == "done"


def test_holder_names_the_app_and_job():
    assert jobs.holder_for(12) == "bench:job-12"
