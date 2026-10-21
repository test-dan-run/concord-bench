"""ensure_services with stack.py mocked (blocked state, log streaming, eviction flag), leases
(SPLIT_PLAN.md §6: holder, renewal, release) and worker batching under util_limit (PLAN §8)."""

from __future__ import annotations

import io
import threading
import time

import pytest

from concord_bench.platform import services
from concord_bench.platform.services import Health, ServicesBlocked
from concord_bench.runs import jobs


class FakeProc:
    def __init__(self, lines: list[str], code: int):
        self.stdout = io.StringIO("".join(f"{x}\n" for x in lines))
        self._code = code

    def wait(self):
        return self._code


@pytest.fixture
def health(monkeypatch):
    state: dict[str, str] = {}
    monkeypatch.setattr(services, "health", lambda n, timeout=2.0: Health(n, state.get(n, "down")))
    return state


def test_all_up_still_takes_the_lease(health, data_dir):
    """`up --holder` is called even when everything is up: that is what leases the services."""
    health.update({"a": "up", "b": "up"})
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc(["leased to bench:job-7 for 120s: a, b"], 0)

    services.ensure_services(["b", "a"], runner=runner, holder="bench:job-7")
    assert seen["cmd"][-5:] == ["up", "a", "b", "--holder", "bench:job-7"]


def test_starts_whole_set_and_streams_log(health, data_dir):
    health.update({"a": "up", "b": "down"})
    seen = {}
    logs: list[str] = []

    def runner(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        health["b"] = "up"
        return FakeProc(["[1/2] a already running", "[2/2] starting b", "up: a, b"], 0)

    services.ensure_services(["b", "a"], logs.append, runner=runner)
    assert seen["cmd"][-3:] == ["up", "a", "b"]  # every required service, so none of them is evicted
    assert "--no-evict" not in seen["cmd"]
    assert "[2/2] starting b" in logs and logs[-1] == "services ready: a, b"


def test_no_evict_flag(health, data_dir, monkeypatch):
    from concord_bench.core.settings import reset_settings

    monkeypatch.setenv("NO_EVICT", "true")
    reset_settings()
    assert services.stack_command(["a"])[-1] == "--no-evict"


def test_failure_is_blocked_with_tool_message(health, data_dir):
    def runner(cmd, **kw):
        return FakeProc(["checking", "error: b: weights not found at /x; run `just fetch-weights b`"], 1)

    with pytest.raises(ServicesBlocked, match="weights not found"):
        services.ensure_services(["b"], runner=runner)


def test_unknown_service_blocks(monkeypatch, data_dir):
    monkeypatch.setattr(services, "health", lambda n, timeout=2.0: Health(n, "unknown", "not in map"))
    with pytest.raises(ServicesBlocked, match="not in the platform endpoint map"):
        services.ensure_services(["ghost"])


def test_blocked_job_state(session, health, monkeypatch):
    def boom(names, log, **kw):
        raise ServicesBlocked("docker daemon not reachable")

    monkeypatch.setattr(services, "ensure_services", boom)
    job = jobs.enqueue(session, "check", {"service": "x"}, ["x"])
    session.commit()
    jobs.run_pending()
    session.refresh(job)
    assert job.status == "blocked" and "docker" in job.error
    assert any("blocked: docker daemon" in line for line in jobs.read_log(job.id))


def test_claim_prefers_jobs_whose_services_are_up_and_batches(session, health, monkeypatch):
    fractions = {"big": 0.6, "mid": 0.3, "small": 0.1, "cpu": 0.0}
    monkeypatch.setattr(services, "gpu_fraction", lambda n: fractions[n])
    monkeypatch.setattr(services, "util_limit", lambda: 0.93)
    health.update({"cpu": "up"})
    j1 = jobs.enqueue(session, "check", {"service": "big"}, ["big"])
    j2 = jobs.enqueue(session, "check", {"service": "cpu"}, ["cpu"])
    jobs.enqueue(session, "check", {"service": "mid"}, ["mid"])
    jobs.enqueue(session, "check", {"service": "small"}, ["small"])
    session.commit()
    job, batch = jobs.claim_next(session)
    assert job.id == j2.id and batch == ["cpu"]  # services already up: no start, no eviction
    job, batch = jobs.claim_next(session)
    assert job.id == j1.id
    assert batch == ["big", "mid"]  # big + mid = 0.9 fits; adding small (1.0) would not


# ---------------------------------------------------------------- leases


@pytest.fixture
def platform(monkeypatch, health):
    """stack.py mocked: `up` through the Popen runner, `lease` / `release` through run_tool."""
    calls: list[list[str]] = []
    state = {"up_code": 0, "up_lines": ["up: a"], "lease": (0, "renewed")}
    lock = threading.Lock()

    def popen(cmd, **kw):
        with lock:
            calls.append(cmd[cmd.index("tools/stack.py") + 1:])
        for n in cmd[cmd.index("up") + 1:]:
            if not n.startswith("-") and state["up_code"] == 0:
                health[n] = "up"
        return FakeProc(state["up_lines"], state["up_code"])

    def run_tool(cmd, timeout=120):
        with lock:
            calls.append(cmd[cmd.index("tools/stack.py") + 1:])
        return state["lease"] if cmd[cmd.index("tools/stack.py") + 1] == "lease" else (0, "released")

    monkeypatch.setattr(services.subprocess, "Popen", popen)
    monkeypatch.setattr(services, "run_tool", run_tool)
    monkeypatch.setattr(services, "LEASE_RENEW_S", 0.02)
    return calls, state


def _lease_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("lease bench:")]


def _job(session, monkeypatch, handler) -> int:
    monkeypatch.setitem(jobs.HANDLERS, "fake", handler)
    job = jobs.enqueue(session, "fake", {}, ["a"])
    session.commit()
    return job.id


def _wait_for(pred, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_job_leases_renews_and_releases_on_success(session, platform, monkeypatch):
    calls, _ = platform
    seen = {}

    def handler(session, job, ctx):
        seen["renewed"] = _wait_for(lambda: ["lease", "--holder", f"bench:job-{job.id}"] in calls)
        seen["threads"] = len(_lease_threads())
        return {"ok": True}

    job_id = _job(session, monkeypatch, handler)
    jobs.run_pending()
    holder = f"bench:job-{job_id}"
    assert calls[0] == ["up", "a", "--holder", holder]
    assert seen == {"renewed": True, "threads": 1}  # the renew thread ran while the job did
    assert calls[-1] == ["release", "--holder", holder]
    assert _lease_threads() == []  # ...and stopped with it
    assert session.get(jobs.Job, job_id).status == "done"


def test_job_releases_on_failure(session, platform, monkeypatch):
    calls, _ = platform

    def handler(session, job, ctx):
        raise RuntimeError("model fell over")

    job_id = _job(session, monkeypatch, handler)
    jobs.run_pending()
    job = session.get(jobs.Job, job_id)
    session.refresh(job)
    assert job.status == "failed" and "fell over" in job.error
    assert calls[-1] == ["release", "--holder", f"bench:job-{job_id}"]
    assert _lease_threads() == []


def test_refused_lease_blocks_the_job_and_releases(session, platform, monkeypatch):
    calls, state = platform
    state["up_code"] = 1
    state["up_lines"] = ["error: not enough VRAM next to leased services qwen3-8b (held by label:job-1); "
                         "wait for it or stop it"]
    job_id = _job(session, monkeypatch, lambda *a: pytest.fail("must not run"))
    jobs.run_pending()
    job = session.get(jobs.Job, job_id)
    session.refresh(job)
    assert job.status == "blocked" and "held by label:job-1" in job.error
    assert calls[-1] == ["release", "--holder", f"bench:job-{job_id}"]
    assert _lease_threads() == []


def test_expired_lease_is_taken_again(session, platform, monkeypatch):
    calls, state = platform
    state["lease"] = (1, "error: no live lease for bench:job-1 (expired or released)")

    def handler(session, job, ctx):
        _wait_for(lambda: sum(c[0] == "up" for c in calls) >= 2)
        return {}

    job_id = _job(session, monkeypatch, handler)
    jobs.run_pending()
    ups = [c for c in calls if c[0] == "up"]
    assert len(ups) >= 2 and all(c[-2:] == ["--holder", f"bench:job-{job_id}"] for c in ups)
    assert calls[-1][0] == "release"


def test_replay_mode_takes_no_lease(session, platform, monkeypatch):
    from concord_bench.core.settings import reset_settings

    calls, _ = platform
    monkeypatch.setenv("REPLAY_DIR", "/nonexistent")
    reset_settings()
    job_id = _job(session, monkeypatch, lambda *a: {})
    jobs.run_pending()
    assert calls == [] and session.get(jobs.Job, job_id).status == "done"
