"""HTTP API on the fixture set (TestClient): uploads, ingest job, variants, registry errors, templates."""

from __future__ import annotations

import io
import zipfile

import pytest

from concord.runs import jobs
from conftest import GEN


def upload_fixture(client, refs="stm") -> str:
    uid = client.post("/api/uploads").json()["upload_id"]
    files, paths = [], []
    for p in sorted((GEN / "dataset" / "audio").iterdir()):
        files.append(("files", (p.name, p.read_bytes())))
        paths.append(("paths", f"calls/audio/{p.name}"))
    for p in (GEN / "refs" / refs).iterdir():
        files.append(("files", (p.name, p.read_bytes())))
        paths.append(("paths", f"calls/{p.name}"))
    r = client.post(f"/api/uploads/{uid}/files", files=files, data=dict(paths=[v for _, v in paths]))
    assert r.status_code == 200, r.text
    assert r.json()["files"] == len(files)
    return uid


def test_config_lite_by_default(client):
    cfg = client.get("/api/config").json()
    assert cfg["features"] == {"improve": False} and cfg["variant"] == "lite"
    assert [t["id"] for t in cfg["tasks"]] == ["asr", "diar", "pipeline"]
    assert client.get("/api/improve/status").status_code == 404


def test_full_variant_mounts_improve(monkeypatch, data_dir):
    from fastapi.testclient import TestClient

    from concord.api.app import create_app
    from concord.core.settings import reset_settings

    monkeypatch.setenv("FEATURE_IMPROVE", "1")
    reset_settings()
    with TestClient(create_app()) as c:
        assert c.get("/api/config").json()["features"]["improve"] is True
        assert c.get("/api/improve/status").json()["state"] == "on hold"


def test_upload_inspect_create_ingest(client, data_dir):
    uid = upload_fixture(client)
    ins = client.get(f"/api/uploads/{uid}/inspect").json()
    assert ins["audio_files"] == 8 and ins["tasks"]["spk_asr"]["files"] == 8
    r = client.post("/api/datasets", json={"name": "Calls", "tags": ["#call-centre", "en", "en"], "language": "en-SG",
                                           "upload_id": uid})
    assert r.status_code == 201, r.text
    ds = r.json()["dataset"]
    assert ds["status"] == "ingesting" and ds["tags"] == ["call-centre", "en"]
    jobs.run_pending()
    d = client.get(f"/api/datasets/{ds['id']}").json()
    assert d["status"] == "ready" and d["usable_files"] == 6 and d["has_speaker_text"]
    assert d["ingest_job"]["status"] == "done"
    assert d["flagged"] == {"clipped": 1, "undecodable": 1, "too_long": 1}
    files = client.get(f"/api/datasets/{ds['id']}/files").json()
    assert {f["status"] for f in files} == {"ok", "clipped", "undecodable", "too_long"}
    ok = next(f for f in files if f["status"] == "ok")
    a = client.get(f"/api/audio/{ok['id']}", headers={"Range": "bytes=0-99"})
    assert a.status_code == 206 and len(a.content) == 100
    assert client.get(f"/api/audio/{ok['id']}/peaks").json()["per_second"] == 100
    assert [t["tag"] for t in client.get("/api/tags").json()] == ["call-centre", "en"]
    assert not (data_dir / "uploads" / uid).exists()  # staged upload cleaned up after ingest


def test_chunked_zip_upload(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(GEN / "dataset" / "audio" / "d04.ogg", "audio/d04.ogg")
        zf.write(GEN / "refs" / "asr-txt" / "d04.txt", "d04.txt")
    blob = buf.getvalue()
    uid = client.post("/api/uploads").json()["upload_id"]
    half = len(blob) // 2
    assert client.put(f"/api/uploads/{uid}/chunk?path=set.zip&offset=0", content=blob[:half]).status_code == 200
    assert client.put(f"/api/uploads/{uid}/chunk?path=set.zip&offset=0", content=blob[half:]).status_code == 409
    assert client.put(f"/api/uploads/{uid}/chunk?path=set.zip&offset={half}", content=blob[half:]).json()["size"] == len(blob)
    ins = client.get(f"/api/uploads/{uid}/inspect").json()
    assert ins["audio_files"] == 1 and ins["tasks"]["asr"]["files"] == 1


def test_unsafe_upload_path_rejected(client):
    uid = client.post("/api/uploads").json()["upload_id"]
    r = client.post(f"/api/uploads/{uid}/files", files=[("files", ("x.wav", b"x"))], data={"paths": ["../../etc/x.wav"]})
    assert r.status_code == 400


def test_local_path_ingest_and_validation(client, tmp_path):
    from concord.acceptance import stage_fixture_dataset

    src = stage_fixture_dataset(tmp_path / "src", refs="rttm")
    assert client.post("/api/uploads/inspect-path", json={"path": "relative/path"}).status_code == 400
    ins = client.post("/api/uploads/inspect-path", json={"path": str(src)}).json()
    assert set(ins["tasks"]) == {"diar"}
    assert client.post("/api/datasets", json={"name": "x"}).status_code == 400
    r = client.post("/api/datasets", json={"name": "Diar only", "local_path": str(src)})
    jobs.run_pending()
    d = client.get(f"/api/datasets/{r.json()['dataset']['id']}").json()
    assert d["label_types"] == ["diar"]
    assert src.exists()  # a local folder is never deleted


def test_templates(client):
    names = {t["name"] for t in client.get("/api/templates").json()}
    assert {"asr.jsonl", "diarisation.rttm", "speaker_transcripts.stm", "README.md"} <= names
    assert client.get("/api/templates/asr.csv").text.startswith("audio_filepath,text")
    assert client.get("/api/templates/..%2Fdataset%2Ftruth.json").status_code == 404


def test_registry_lists_platform_entries(client):
    entries = client.get("/api/registry").json()
    by = {e["name"]: e for e in entries}
    if not by:
        pytest.skip("no endpoint map")
    assert by["silero-vad"]["usable_as"] == "segmenter" and by["silero-vad"]["relevance"] == 0
    assert by["text-normalizer"]["usable_as"] is None
    assert entries[0]["relevance"] == 0  # speech models first
    assert client.post("/api/registry/nope/check").status_code == 404


def test_model_requires_successful_check(client, session):
    job = jobs.enqueue(session, "check", {"service": "silero-vad"})
    job.status = "done"
    job.result = {"report": {"capabilities": [], "timing_level": None}}
    session.commit()
    r = client.post("/api/models", json={"registry_name": "silero-vad", "check_job_id": job.id})
    assert r.status_code == 422
    r = client.post("/api/models", json={"registry_name": "default-vad", "check_job_id": 999})
    assert r.status_code == 400
