"""Shared fixtures. Every test gets its own CONCORD_DATA and database; nothing needs the platform."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "fixtures" / "generated"
RECORDED = ROOT / "fixtures" / "recorded"


def truth() -> dict:
    return json.loads((GEN / "dataset" / "truth.json").read_text())


def dialogue_files() -> dict:
    """The six usable dialogues (no corrupt / long file)."""
    return {k: v for k, v in truth().items() if k.startswith("d0") and k not in ("d07_corrupt", "d08_long")}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from concord_bench.core import db
    from concord_bench.core.settings import reset_settings

    monkeypatch.setenv("CONCORD_DATA", str(tmp_path / "data"))
    for var in ("REPLAY_DIR", "RECORD_DIR", "NO_EVICT"):
        monkeypatch.delenv(var, raising=False)
    reset_settings()
    db.reset_engines()
    db.migrate()
    yield tmp_path / "data"
    db.reset_engines()
    reset_settings()


@pytest.fixture
def session(data_dir):
    from concord_bench.core.db import SessionLocal

    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def client(data_dir):
    from fastapi.testclient import TestClient

    from concord_bench.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def replay(monkeypatch, data_dir):
    """Recorded responses instead of live services, with the endpoint map they were recorded with."""
    from concord_bench.core.settings import reset_settings

    if not (RECORDED / "endpoints.generated.yaml").is_file():
        pytest.skip("no recorded responses; run `just record`")
    monkeypatch.setenv("REPLAY_DIR", str(RECORDED))
    monkeypatch.setenv("AICORE_ENDPOINTS", str(RECORDED / "endpoints.generated.yaml"))
    reset_settings()
    yield RECORDED
    reset_settings()
