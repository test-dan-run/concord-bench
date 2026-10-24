"""HTTP API on the fixture set (TestClient): config, uploads, ingest job, flags, catalog models, outputs
served to other apps, templates."""

from __future__ import annotations

import io
import json
import shutil
import zipfile

from concord_bench.runs import jobs
from conftest import GEN, ROOT


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


def test_config_names_the_app_and_contract(client):
    """CONTRACTS.md §1: app, version, contract version and peer (no Label link yet)."""
    cfg = client.get("/api/config").json()
    assert cfg["app"] == "concord-bench" and cfg["version"] == "0.2.0" and cfg["contract_version"] == 3
    assert cfg["peer"] == {"url": None, "reachable": False, "contract_version": None}
    sample = json.loads((ROOT / "contracts" / "samples" / "api" / "config.bench.json").read_text())
    assert set(sample) <= set(cfg) and set(sample["peer"]) == set(cfg["peer"])
    assert "features" not in cfg and "variant" not in cfg and "no_evict" not in cfg
    assert cfg["catalog"] == {"url": "http://127.0.0.1:8700", "ui_url": "http://127.0.0.1:8700"}
    assert [t["id"] for t in cfg["tasks"]] == ["asr", "diar", "pipeline"]
    for gone in ("/api/improve/status", "/api/label/console", "/api/sync/packages"):
        assert client.get(gone).status_code == 404


def test_upload_inspect_create_ingest(client, data_dir):
    uid = upload_fixture(client)
    ins = client.get(f"/api/uploads/{uid}/inspect").json()
    assert ins["audio_files"] == 7 and ins["tasks"]["spk_asr"]["files"] == 7
    r = client.post("/api/datasets", json={"name": "Calls", "tags": ["#call-centre", "en", "en"], "language": "en-SG",
                                           "upload_id": uid})
    assert r.status_code == 201, r.text
    ds = r.json()["dataset"]
    assert ds["status"] == "ingesting" and ds["tags"] == ["call-centre", "en"]
    jobs.run_pending()
    d = client.get(f"/api/datasets/{ds['id']}").json()
    assert d["status"] == "ready" and d["usable_files"] == 6 and d["has_speaker_text"]
    assert d["ingest_job"]["status"] == "done"
    assert d["flagged"] == {"clipped": 1, "undecodable": 1}
    files = client.get(f"/api/datasets/{ds['id']}/files").json()
    assert {f["status"] for f in files} == {"ok", "clipped", "undecodable"}
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
    from concord_bench.acceptance import stage_fixture_dataset

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


def test_models_come_from_the_catalog(client, session):
    """The Models page lists the catalog's speech models with state, capabilities, cold start and build key;
    there is nothing to register, re-check or remove in Bench."""
    from aicore.catalog import FakeCatalog

    from concord_bench.core.models import Model
    from concord_bench.platform import catalog
    from conftest import catalog_model

    fake = FakeCatalog([
        catalog_model("faster-whisper-large-v3", ["asr"], contract="openai", api="audio.transcriptions",
                      timing="word", state="stopped", tags=["call-centre"]),
        catalog_model("pyannote-diarization", ["diar"], cold_start_s=21.0, cold_start_measured=True),
        catalog_model("new-asr", ["asr"], contract="openai", api="audio.transcriptions", status="pending"),
        {**catalog_model("text-normalizer", []), "api": "text.transform", "capabilities": None},
        {**catalog_model("unregistered-vad", ["vad"]), "registered": False},
    ])
    catalog.use(fake)
    body = client.get("/api/models").json()
    assert body["catalog"]["available"] and body["catalog"]["ui_url"] == "http://127.0.0.1:8700"
    by = {m["catalog_id"]: m for m in body["models"]}
    assert set(by) == {"faster-whisper-large-v3", "pyannote-diarization", "new-asr"}
    fw = by["faster-whisper-large-v3"]
    assert fw["capabilities"] == ["asr"] and fw["timing_level"] == "word" and fw["state"] == "stopped"
    assert fw["usable"] and fw["build_key"] == "b000000000000001" and fw["model_id"] is None  # no row until used
    assert by["pyannote-diarization"]["has_speakers"] and by["pyannote-diarization"]["cold_start_s"] == 21.0
    assert not by["new-asr"]["usable"] and "pending" in by["new-asr"]["not_usable"]
    for gone in (("post", "/api/models"), ("post", "/api/registry/silero-vad/check"), ("get", "/api/registry"),
                 ("post", "/api/models/1/recheck")):
        assert getattr(client, gone[0])(gone[1]).status_code in (404, 405)
    # a model gets its row on first use, and pickers rank catalog models by shared tags
    m = catalog.ensure_model(session, "faster-whisper-large-v3")
    session.commit()
    assert m.is_asr and m.timing_level == "word" and m.tags == ["call-centre"]
    sug = client.get("/api/suggestions/models?tags=call-centre&label_types=asr").json()
    assert [(e["catalog_id"], e["preselect"]) for e in sug["asr"]] == [("faster-whisper-large-v3", True),
                                                                       ("new-asr", False)]
    assert client.get("/api/models").json()["models"][0]["model_id"] == m.id
    # the catalog is down: the models Bench has used, from their last record
    class Down(FakeCatalog):
        def models(self, *a, **k):
            import httpx

            raise httpx.ConnectError("refused")

    catalog.use(Down())
    body = client.get("/api/models").json()
    assert not body["catalog"]["available"] and "not reachable" in body["catalog"]["error"]
    assert [(x["catalog_id"], x["capabilities"], x["in_catalog"]) for x in body["models"]] == [
        ("faster-whisper-large-v3", ["asr"], False)]
    assert session.query(Model).count() == 1
    assert [n["id"] for n in client.get("/api/normalisers").json()] == ["basic", "whisper_en", "none"]


def test_outputs_served_to_other_apps(client, session):
    """POST /api/outputs/batch (platform/catalog/README.md): rows by id, missing ids left out."""
    from concord_bench.core.models import ModelOutput

    row = ModelOutput(model_name="silero-vad", build_key="b000000000000001", audio_sha256="ab" * 32,
                      span_start=-1.0, span_end=-1.0, params_hash="e3c3f2d4fcaebd2c",
                      params={"adapter": "OIPSegmenter"}, output={"segments": [[0.5, 1.0]], "speakers": None},
                      latency_s=0.25)
    session.add(row)
    session.commit()
    body = client.post("/api/outputs/batch", json={"ids": [row.id, 999, str(row.id)]}).json()
    assert body == {"outputs": [{"id": row.id, "build_key": "b000000000000001", "audio_sha256": "ab" * 32,
                                 "span_start": -1.0, "span_end": -1.0, "params_hash": "e3c3f2d4fcaebd2c",
                                 "params": {"adapter": "OIPSegmenter"},
                                 "output": {"segments": [[0.5, 1.0]], "speakers": None}, "latency_s": 0.25}]}


def test_app_registers_with_the_catalog(data_dir, fake_catalog):
    import time

    from fastapi.testclient import TestClient

    from concord_bench.api.app import create_app

    with TestClient(create_app()):
        for _ in range(100):
            if fake_catalog.apps():
                break
            time.sleep(0.01)
    assert fake_catalog.apps() == [{"name": "bench", "base_url": "http://127.0.0.1:8600"}]


def test_audio_only_upload_refused(client, tmp_path):
    """Bench needs references: a folder of audio alone is refused before anything is stored."""
    src = tmp_path / "audio-only"
    shutil.copytree(GEN / "dataset" / "audio", src / "audio")
    ins = client.post("/api/uploads/inspect-path", json={"path": str(src)}).json()
    assert ins["audio_files"] == 7 and ins["tasks"] == {}
    r = client.post("/api/datasets", json={"name": "Unlabelled", "local_path": str(src)})
    assert r.status_code == 400 and "no references found" in r.json()["detail"]
    assert "Concord Label" in r.json()["detail"]
    assert client.get("/api/datasets").json() == []


def test_flagged_references_listed_on_dataset(client, tmp_path):
    from concord_bench.acceptance import stage_fixture_dataset
    from concord_bench.core.db import SessionLocal
    from concord_bench.core.models import Segment

    src = stage_fixture_dataset(tmp_path / "src", refs="stm")
    ds = client.post("/api/datasets", json={"name": "Calls", "local_path": str(src)}).json()["dataset"]
    jobs.run_pending()
    assert client.get(f"/api/datasets/{ds['id']}/flags").json() == []
    with SessionLocal() as s:
        seg_id = s.query(Segment.id).order_by(Segment.id).first()[0]
    r = client.post(f"/api/segments/{seg_id}/flag", json={"flagged": True, "note": "wrong speaker"})
    assert r.json() == {"id": seg_id, "flagged": True, "note": "wrong speaker"}
    flags = client.get(f"/api/datasets/{ds['id']}/flags").json()
    assert [(f["id"], f["note"]) for f in flags] == [(seg_id, "wrong speaker")]
    assert len(flags[0]["uid"]) == 32 and len(flags[0]["sha256"]) == 64
    client.post(f"/api/segments/{seg_id}/flag", json={"flagged": False})
    assert client.get(f"/api/datasets/{ds['id']}/flags").json() == []
    assert client.get("/api/datasets/999/flags").status_code == 404
