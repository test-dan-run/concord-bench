"""ensure_services with stack.py mocked (blocked state, log streaming, eviction flag) and worker
batching under util_limit (PLAN §8)."""

from __future__ import annotations

import io

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


def test_all_up_does_nothing(health, data_dir):
    health.update({"a": "up", "b": "up"})
    services.ensure_services(["a", "b"], runner=lambda *a, **k: pytest.fail("must not start anything"))


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
