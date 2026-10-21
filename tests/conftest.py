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
    for var in ("REPLAY_DIR", "RECORD_DIR", "FEATURE_IMPROVE", "NO_EVICT"):
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


# ---------------------------------------------------------------- Label (Phase 2)

@pytest.fixture
def label_flow(replay, client, tmp_path):
    """The Phase 1 acceptance flow, plus an audio-only copy of the same audio ("unlabelled")."""
    from concord_bench.acceptance import run_flow
    from label_helpers import audio_only_dataset

    out = run_flow(client, tmp_path / "staging", log=lambda m: None)
    out["ids"] = {n: m["id"] for n, m in out["models"].items()}
    out["unlabelled"] = audio_only_dataset(client, tmp_path)
    return out


@pytest.fixture
def labelled(label_flow, client):
    """The label flow plus one pseudo-label run (pyannote -> both Whispers) on the audio-only dataset."""
    from label_helpers import label

    started, res = label(client, label_flow)
    label_flow["started"], label_flow["results"] = started, res
    return label_flow
