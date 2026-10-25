"""Shared fixtures. Every test gets its own CONCORD_DATA and database; nothing needs the platform or the
Model Catalog (an in-memory `FakeCatalog` stands in, or the replay stand-in built from the recording)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from concord_bench.core.settings import REPO_ROOT, Settings

# Tests don't read .env, which points at the services on this machine (LABEL_URL, REPLAY_DIR, DATASET_STORE).
# `just` exports it, so its values leave the environment too. Only PLATFORM_DIR stays, for the platform's
# contract samples.
_dotenv = dotenv_values(REPO_ROOT / ".env")
for _key, _value in _dotenv.items():
    if _key != "PLATFORM_DIR" and os.environ.get(_key) == _value:
        del os.environ[_key]
if not os.environ.get("PLATFORM_DIR") and _dotenv.get("PLATFORM_DIR"):
    os.environ["PLATFORM_DIR"] = _dotenv["PLATFORM_DIR"]
Settings.model_config["env_file"] = None

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "fixtures" / "generated"
RECORDED = ROOT / "fixtures" / "recorded"


def truth() -> dict:
    return json.loads((GEN / "dataset" / "truth.json").read_text())


def dialogue_files() -> dict:
    """The six usable dialogues (no corrupt / long file)."""
    return {k: v for k, v in truth().items() if k.startswith("d0") and k not in ("d07_corrupt", "d08_long")}


@pytest.fixture(autouse=True)
def fake_catalog(request):
    """No test reaches a real Model Catalog: an empty in-memory FakeCatalog answers unless a test installs
    its own (`catalog.use`) or replays recordings (the `replay` fixture); live tests use the real one."""
    from aicore.catalog import FakeCatalog

    from concord_bench.platform import catalog
    from concord_bench.runs import jobs

    if request.node.get_closest_marker("live"):
        catalog.reset()
        yield None
        return
    fake = FakeCatalog()
    catalog.use(fake)
    yield fake
    for lease in jobs._waiting.values():
        lease.release()
    jobs._waiting.clear()
    catalog.reset()


def catalog_model(model_id: str, tasks: list[str], *, contract: str = "oip-v2", api: str = "infer",
                  timing: str | None = "segment", build_key: str = "b000000000000001", state: str = "running",
                  status: str = "verified", **extra) -> dict:
    """A catalog model record (platform/catalog/README.md) for FakeCatalog."""
    caps = {"build_key": build_key, "status": status, "tasks": tasks, "timing_level": timing,
            "usable_as": "asr" if "asr" in tasks else "segmenter"}
    return {"id": model_id, "aliases": [], "registered": True, "display_name": model_id, "tags": [],
            "contract": contract, "api": api, "state": state, "build_key": build_key,
            "image": f"ghcr.io/owner/{model_id}:0.1.0", "capabilities": caps, "cold_start_s": 30.0,
            "cold_start_measured": False, **extra}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from concord_bench import peer
    from concord_bench.core import db
    from concord_bench.core.settings import reset_settings

    monkeypatch.setenv("CONCORD_DATA", str(tmp_path / "data"))
    for var in ("REPLAY_DIR", "RECORD_DIR", "LABEL_URL", "CATALOG_URL", "AICORE_CATALOG", "CATALOG_UI_URL", "PUBLIC_URL",
                "DATASET_STORE"):
        monkeypatch.delenv(var, raising=False)
    reset_settings()
    peer.clear_cache()
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


def install_dataset_catalog(monkeypatch, root: Path):
    """The Dataset Catalog set up: DATASET_STORE is the folder of an in-memory FakeDatasetCatalog (aicore),
    which answers as the catalog service. `reset_dataset_catalog` takes it away after the test."""
    from aicore.datasets import FakeDatasetCatalog

    from concord_bench import APP
    from concord_bench.core.settings import reset_settings
    from concord_bench.platform import dataset_catalog

    fake = FakeDatasetCatalog(root, app=APP)
    monkeypatch.setenv("DATASET_STORE", str(fake.store))
    reset_settings()
    dataset_catalog.use(fake)
    return fake


@pytest.fixture(autouse=True)
def reset_dataset_catalog():
    yield
    from concord_bench.platform import dataset_catalog

    dataset_catalog.reset()


@pytest.fixture
def dataset_store(tmp_path, monkeypatch, data_dir):
    return install_dataset_catalog(monkeypatch, tmp_path / "store")


@pytest.fixture
def replay(monkeypatch, data_dir):
    """Recorded responses instead of live services, with the endpoint map they were recorded with; the
    Model Catalog is the replay stand-in built from that map and summary.json."""
    from concord_bench.core.settings import reset_settings
    from concord_bench.platform import catalog

    if not (RECORDED / "endpoints.generated.yaml").is_file():
        pytest.skip("no recorded responses; run `just record`")
    monkeypatch.setenv("REPLAY_DIR", str(RECORDED))
    monkeypatch.setenv("AICORE_ENDPOINTS", str(RECORDED / "endpoints.generated.yaml"))
    reset_settings()
    catalog.use(None)
    yield RECORDED
    reset_settings()
