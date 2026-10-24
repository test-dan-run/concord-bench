"""Reference packages into Bench (CONTRACTS.md §2, §4; SPLIT_PLAN.md §4.3, §10): every sample package
imports with the expected segments, label types and provenance; versions; refusals; outputs into the
cache; the flags file; the push endpoints gone; scores by catalog dataset; the peer in /api/config."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from concord_bench import CONTRACT_VERSION
from concord_bench.core.models import AudioFile, Dataset, Model, ModelOutput, ReferenceImport, Run, Score, Segment
from concord_bench.runs import jobs
from conftest import GEN, ROOT, truth

SAMPLES = ROOT / "contracts" / "samples"
API = SAMPLES / "api"
CLIP_SHA = "36d978579f0dd2ee634aff2674938f2c40089bce9ce624eca26a9a30d1830eea"
LABEL_UIDS = ["a1b2c3d4e5f60718293a4b5c6d7e8f90", "b2c3d4e5f60718293a4b5c6d7e8f90a1",
              "c3d4e5f60718293a4b5c6d7e8f90a1b2"]


def _checker():
    spec = importlib.util.spec_from_file_location("contracts_check", ROOT / "contracts" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sample(name: str) -> dict:
    return json.loads((API / name).read_text())


def upload_folder(client, folder: Path) -> str:
    """A package folder through the multipart upload, as the folder picker sends it."""
    uid = client.post("/api/uploads").json()["upload_id"]
    files, rels = [], []
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            files.append(("files", (p.name, p.read_bytes())))
            rels.append(f"{folder.name}/{p.relative_to(folder).as_posix()}")
    r = client.post(f"/api/uploads/{uid}/files", files=files, data={"paths": rels})
    assert r.status_code == 200, r.text
    return uid


def upload_zip(client, blob: bytes, name: str = "package.zip") -> str:
    uid = client.post("/api/uploads").json()["upload_id"]
    r = client.put(f"/api/uploads/{uid}/chunk?path={name}&offset=0", content=blob)
    assert r.status_code == 200, r.text
    return uid


def zip_folder(folder: Path, top: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                rel = p.relative_to(folder).as_posix()
                zf.write(p, f"{top}/{rel}" if top else rel)
    return buf.getvalue()


def stage(client, folder: Path) -> dict:
    r = client.post("/api/reference-imports", json={"upload_id": upload_folder(client, folder)})
    assert r.status_code == 201, r.text
    return r.json()


def accept(client, import_id: int, **body) -> dict:
    r = client.post(f"/api/reference-imports/{import_id}/accept", json=body or None)
    assert r.status_code == 202, r.text
    jobs.run_pending()
    out = r.json()
    d = client.get(f"/api/datasets/{out['dataset']['id']}").json()
    assert d["status"] == "ready", d
    return d


def segments_of(session, dataset_id: int) -> list[Segment]:
    session.expire_all()
    return list(session.scalars(select(Segment).join(AudioFile).where(AudioFile.dataset_id == dataset_id)
                                .order_by(Segment.start)))


def import_complete(client) -> dict:
    staged = stage(client, SAMPLES / "reference-package-complete")
    return accept(client, staged["id"])


# ---------------------------------------------------------------- the sample packages


def test_complete_package_preview(client):
    staged = stage(client, SAMPLES / "reference-package-complete")
    manifest = json.loads((SAMPLES / "reference-package-complete" / "manifest.json").read_text())
    assert staged["status"] == "staged"
    want = {"name": "Help desk calls", "source": manifest["source"], "export_version": 1, "files": 1, "new_files": 1,
            "hours": 0.002933, "segments": 3, "label_types": ["asr", "diar"], "previous_version": None}
    assert {k: staged["preview"][k] for k in want} == want
    assert staged["preview"]["outputs"] == 1 and staged["preview"]["uids"] == 3
    listed = client.get("/api/reference-imports?status=staged").json()
    assert [i["id"] for i in listed] == [staged["id"]]
    assert client.get(f"/api/reference-imports/{staged['id']}").json()["preview"]["dataset_name"] == "Help desk calls"


def test_complete_package_accepts_into_a_dataset_with_provenance(client, session, data_dir):
    staged = stage(client, SAMPLES / "reference-package-complete")
    upload_id = session.get(ReferenceImport, staged["id"]).upload_id
    d = accept(client, staged["id"])
    assert d["name"] == "Help desk calls" and d["label_types"] == ["asr", "diar"] and d["has_speaker_text"]
    assert d["language"] == "en" and d["tags"] == ["call-centre", "en"]
    prov = d["provenance"]
    assert prov["source"]["dataset_uid"] == "8d4e2b6a1c9f4e3a9b7c5d1e2f3a4b5c" and prov["export_version"] == 1
    assert prov["scope"] == "complete" and prov["import_id"] == staged["id"] and prov["normaliser"] == "basic"
    assert prov["review"]["confirmed"] == 3 and "whisper-large-v3" in prov["models"]
    assert d["export_version"] == 1 and d["previous_version_id"] is None and d["previous_plan"] is None
    assert [v["id"] for v in d["versions"]] == [d["id"]]
    files = client.get(f"/api/datasets/{d['id']}/files").json()
    assert [(f["path"], f["sha256"]) for f in files] == [("check_clip.wav", CLIP_SHA)]  # the files.csv path
    segs = segments_of(session, d["id"])
    assert [(s.start, s.end, s.speaker) for s in segs] == [(0.4, 3.4, "agent"), (2.9, 6.81, "caller"),
                                                          (7.11, 9.96, "agent")]
    assert [s.uid for s in segs] == LABEL_UIDS  # refs/uids.jsonl
    imp = client.get(f"/api/reference-imports/{staged['id']}").json()
    assert imp["status"] == "accepted" and imp["dataset_id"] == d["id"]
    assert imp["result"]["uids"] == {"matched": 3, "unmatched": 0}
    # outputs/seg.jsonl: pyannote-diarization is not in this (empty) model catalog, so it is skipped
    assert imp["result"]["outputs"]["imported"] == 0 and imp["result"]["outputs"]["skipped"]["unknown_model"] == 1
    assert not (data_dir / "uploads" / upload_id).exists() and not (data_dir / "imports" / str(staged["id"])).exists()
    # the manifest, files.csv, uids and outputs were never read as annotations
    assert set(d["report"]["tasks"]) == {"spk_asr"} and d["report"]["unmatched"] == []
    assert client.post(f"/api/reference-imports/{staged['id']}/accept").status_code == 409


def test_v2_without_audio_is_a_new_dataset_linked_to_v1(client, session):
    v1 = import_complete(client)
    # a score on v1, to check it is untouched by v2
    run = Run(task="asr", dataset_id=v1["id"], status="done")
    session.add(run)
    session.flush()
    session.add(Score(run_id=run.id, normaliser="basic@1", collar=0.25, aggregates={"wer": 0.1}))
    session.commit()
    before = [(s.id, s.uid, s.start, s.end, s.text) for s in segments_of(session, v1["id"])]

    staged = stage(client, SAMPLES / "reference-package-any")  # audio_included "none"
    p = staged["preview"]
    assert p["export_version"] == 2 and p["new_files"] == 0 and p["files"] == 1 and p["segments"] == 2
    assert p["previous_version"] == {"dataset_id": v1["id"], "name": "Help desk calls", "export_version": 1}
    assert p["dataset_name"] == "Help desk calls v2" and p["changes"]["segments_before"] == 3
    v2 = accept(client, staged["id"])
    assert v2["id"] != v1["id"] and v2["name"] == "Help desk calls v2" and v2["label_types"] == ["asr"]
    assert v2["previous_version_id"] == v1["id"] and v2["provenance"]["scope"] == "any"
    assert [v["id"] for v in v2["versions"]] == [v1["id"], v2["id"]]
    assert [s.text for s in segments_of(session, v2["id"])][0].startswith("Thank you for calling")
    assert len(segments_of(session, v2["id"])) == 2
    # v1: same segments, same score
    assert [(s.id, s.uid, s.start, s.end, s.text) for s in segments_of(session, v1["id"])] == before
    session.expire_all()
    assert session.scalar(select(Score).where(Score.run_id == run.id)).aggregates == {"wer": 0.1}
    assert client.get(f"/api/datasets/{v1['id']}").json()["name"] == "Help desk calls"
    plan = v2["previous_plan"]
    assert plan["previous"]["id"] == v1["id"]


def test_diar_package_without_source_or_audio(client, session):
    r = client.post("/api/reference-imports", json={"upload_id": upload_folder(client, SAMPLES / "reference-package-diar")})
    assert r.status_code == 400 and "check_clip.wav" in r.json()["detail"]  # Bench does not have the audio yet
    import_complete(client)
    staged = stage(client, SAMPLES / "reference-package-diar")
    assert staged["preview"]["source"] is None and staged["preview"]["previous_version"] is None
    d = accept(client, staged["id"])
    assert d["name"] == "Help desk calls (speakers)" and d["label_types"] == ["diar"]
    assert d["provenance"]["source"] is None and d["export_version"] is None and d["versions"] == []
    segs = segments_of(session, d["id"])
    assert [(s.start, round(s.end, 3), s.speaker, s.text) for s in segs] == [
        (0.4, 3.4, "agent", None), (2.9, 6.81, "caller", None), (7.11, 9.96, "agent", None)]
    assert client.get(f"/api/datasets/{d['id']}/flags.json").status_code == 409  # no source, no flags round trip


def test_handmade_package_is_read_like_an_annotated_upload(client, session):
    staged = stage(client, SAMPLES / "reference-package-handmade")
    p = staged["preview"]
    assert p["has_manifest"] is False and p["source"] is None and p["segments"] == 3 and p["files"] == 1
    assert p["label_types"] == ["asr", "diar"] and p["hours"] == pytest.approx(10.56 / 3600, abs=1e-4)
    d = accept(client, staged["id"], name="Hand made", tags=["#x"])
    assert d["name"] == "Hand made" and d["tags"] == ["x"] and d["provenance"] == {}
    assert len(segments_of(session, d["id"])) == 3


def test_template_package_imports_as_a_zip(client, session):
    tpl = GEN / "templates" / "reference_package.zip"
    r = client.post("/api/reference-imports", json={"upload_id": upload_zip(client, tpl.read_bytes(), tpl.name)})
    assert r.status_code == 201, r.text
    p = r.json()["preview"]
    assert p["name"] == "Template reference package" and p["label_types"] == ["asr", "diar"] and p["segments"] == 3
    d = accept(client, r.json()["id"])
    assert len(segments_of(session, d["id"])) == 3 and d["label_types"] == ["asr", "diar"]
    names = {t["name"] for t in client.get("/api/templates").json()}
    assert "reference_package.zip" in names


def test_template_package_follows_the_contract(tmp_path):
    with zipfile.ZipFile(GEN / "templates" / "reference_package.zip") as zf:
        zf.extractall(tmp_path)
    (root,) = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert _checker().check_package(root) == []


def test_zip_with_a_top_level_folder(client):
    blob = zip_folder(SAMPLES / "reference-package-complete", top="help-desk-refs-v1")
    r = client.post("/api/reference-imports", json={"upload_id": upload_zip(client, blob)})
    assert r.status_code == 201 and r.json()["preview"]["segments"] == 3


# ---------------------------------------------------------------- refusals


def _copy_complete(tmp_path: Path) -> Path:
    dest = tmp_path / "pkg"
    shutil.copytree(SAMPLES / "reference-package-complete", dest)
    return dest


# ---------------------------------------------------------------- labels (format version 2)

def test_file_and_speaker_labels_from_attributes_json(client, session):
    staged = stage(client, SAMPLES / "reference-package-complete")
    assert staged["preview"]["labels"] == ["channel", "condition", "speaker.accent.region", "speaker.gender"]
    d = accept(client, staged["id"])
    f = session.scalar(select(AudioFile).where(AudioFile.dataset_id == d["id"]))
    assert f.meta == {"condition": "clean", "channel": "phone"}
    segs = segments_of(session, d["id"])
    assert [(s.speaker, s.meta.get("speaker.gender"), s.meta.get("speaker.accent.region")) for s in segs] == [
        ("agent", "F", "south"), ("caller", "M", "north"), ("agent", "F", "south")]
    imp = client.get(f"/api/reference-imports/{staged['id']}").json()
    assert imp["result"]["attributes"] == {"files": 1, "segments": 3}
    labels = {lb["key"]: lb for lb in d["report"]["labels"]}
    assert labels["condition"]["level"] == "file" and labels["condition"]["have"] == 3
    assert labels["speaker.gender"]["level"] == "utterance"
    assert [v["value"] for v in labels["speaker.gender"]["values"]] == ["F", "M"]


def test_labels_on_records_are_read_without_the_attributes_name(client, session):
    import_complete(client)  # the "any" sample carries no audio
    staged = stage(client, SAMPLES / "reference-package-any")
    assert staged["preview"]["labels"] == ["noise"]
    d = accept(client, staged["id"])
    assert [s.meta.get("noise") for s in segments_of(session, d["id"])] == ["street", "none"]
    assert [lb["key"] for lb in d["report"]["labels"]] == ["noise"]


def test_a_record_label_wins_over_a_speaker_or_file_label(client, session, tmp_path):
    pkg = _copy_complete(tmp_path)
    refs = json.loads((pkg / "refs" / "segments.seglst.json").read_text())
    refs[0]["attributes"] = {"speaker": {"gender": "X"}, "condition": "noisy"}
    (pkg / "refs" / "segments.seglst.json").write_text(json.dumps(refs))
    d = accept(client, stage(client, pkg)["id"])
    first = segments_of(session, d["id"])[0]
    assert first.meta["speaker.gender"] == "X" and first.meta["condition"] == "noisy"
    f = session.scalar(select(AudioFile).where(AudioFile.dataset_id == d["id"]))
    assert f.meta["condition"] == "clean"  # the file label; the record's own label applies to its segment


@pytest.mark.parametrize(("attrs", "problem"), [
    ({"files": {"nope.wav": {"a": 1}}}, "files.nope.wav is not a files.csv path"),
    ({"speakers": {"check_clip.wav": {"agent": "F"}}}, "must map each speaker to an object of labels"),
    ({"files": {"check_clip.wav": {"a": [1, 2]}}}, "must be an object of labels"),
    (["not", "an", "object"], "must be an object with files and / or speakers"),
])
def test_bad_attributes_json_is_refused(client, tmp_path, attrs, problem):
    pkg = _copy_complete(tmp_path)
    (pkg / "refs" / "attributes.json").write_text(json.dumps(attrs))
    r = client.post("/api/reference-imports", json={"upload_id": upload_folder(client, pkg)})
    assert r.status_code == 400 and problem in r.json()["detail"], r.text
    assert _checker().check_package(pkg)  # the contract checker agrees


def test_format_version_1_still_imports_and_has_no_attributes_file(client, session, tmp_path):
    pkg = _copy_complete(tmp_path)
    m = json.loads((pkg / "manifest.json").read_text())
    m["format_version"] = 1
    (pkg / "manifest.json").write_text(json.dumps(m))
    r = client.post("/api/reference-imports", json={"upload_id": upload_folder(client, pkg)})
    assert r.status_code == 400 and "exactly one references file besides uids.jsonl (found 2)" in r.json()["detail"]
    (pkg / "refs" / "attributes.json").unlink()
    d = accept(client, stage(client, pkg)["id"])
    assert d["report"]["labels"] == [] and len(segments_of(session, d["id"])) == 3


def test_unknown_format_version_is_refused_naming_the_known_ones(client, tmp_path, data_dir):
    pkg = _copy_complete(tmp_path)
    m = json.loads((pkg / "manifest.json").read_text())
    m["format_version"] = 4
    (pkg / "manifest.json").write_text(json.dumps(m))
    upload_id = upload_zip(client, zip_folder(pkg))
    r = client.post("/api/reference-imports", json={"upload_id": upload_id})
    assert r.status_code == 400
    assert "format_version 4" in r.json()["detail"] and "format version 1, 2, 3" in r.json()["detail"]
    assert client.get("/api/reference-imports").json() == []
    assert not (data_dir / "uploads" / upload_id).exists()  # a refused upload is not kept


def test_audio_that_does_not_match_its_sha256_is_refused(client, tmp_path):
    pkg = _copy_complete(tmp_path)
    wav = pkg / "audio" / "check_clip.wav"
    wav.write_bytes(wav.read_bytes() + b"\0\0")
    r = client.post("/api/reference-imports", json={"upload_id": upload_folder(client, pkg)})
    assert r.status_code == 400 and "audio/check_clip.wav" in r.json()["detail"] and "sha256" in r.json()["detail"]


def test_bad_manifest_fields_are_listed(client, tmp_path):
    pkg = _copy_complete(tmp_path)
    m = json.loads((pkg / "manifest.json").read_text())
    del m["counts"]
    m["label_types"] = ["kws"]
    (pkg / "manifest.json").write_text(json.dumps(m))
    (pkg / "refs" / "extra.rttm").write_text("")
    detail = client.post("/api/reference-imports", json={"upload_id": upload_folder(client, pkg)}).json()["detail"]
    assert "missing counts" in detail and "label_types" in detail and "exactly one references file" in detail


def test_reject_discards_the_package(client, session, data_dir):
    staged = stage(client, SAMPLES / "reference-package-complete")
    upload_id = session.get(ReferenceImport, staged["id"]).upload_id
    r = client.post(f"/api/reference-imports/{staged['id']}/reject")
    assert r.json()["status"] == "rejected"
    assert not (data_dir / "uploads" / upload_id).exists()
    assert client.post(f"/api/reference-imports/{staged['id']}/accept").status_code == 409
    assert client.get("/api/datasets").json() == []


# ---------------------------------------------------------------- outputs into the cache


SAMPLE_BUILD = "a41c9e07d2b85f63"  # the build key in the sample's outputs/seg.jsonl


def _catalog_with_seg(session, build_key: str = SAMPLE_BUILD) -> Model:
    """A catalog whose pyannote-diarization runs the build the sample package's output came from."""
    from aicore.catalog import FakeCatalog

    from concord_bench.platform import catalog
    from conftest import catalog_model

    fake = FakeCatalog([catalog_model("pyannote-diarization", ["diar"], build_key=build_key),
                        catalog_model("silero-vad", ["vad"])])
    catalog.use(fake)
    return catalog.ensure_model(session, "pyannote-diarization")


def test_outputs_import_makes_a_seg_run_a_cache_hit(replay, client, session):
    from concord_bench import adapters
    from concord_bench.api.runs import enqueue_run
    from concord_bench.platform import catalog

    model = _catalog_with_seg(session)
    session.commit()
    d = import_complete(client)
    imp = client.get("/api/reference-imports").json()[0]
    assert imp["result"]["outputs"]["imported"] == 1
    row = session.scalar(select(ModelOutput).where(ModelOutput.audio_sha256 == CLIP_SHA))
    assert row.params_hash == adapters.for_model(model).params_hash and (row.span_start, row.span_end) == (-1.0, -1.0)
    assert row.build_key == SAMPLE_BUILD and row.image == "ghcr.io/owner/pyannote-diarization:0.1.0"
    assert row.output == {"segments": [[0.41, 3.38], [2.93, 6.8], [7.12, 9.94]], "speakers": ["spk0", "spk1", "spk0"]}
    # recorded in the catalog's index, so Concord Label finds it too
    hit = catalog.get().index_lookup("label", [{"build_key": SAMPLE_BUILD, "audio_sha256": CLIP_SHA, "span_start": -1,
                                                "span_end": -1, "params_hash": row.params_hash}])[0]
    assert hit["owner"] == "bench" and hit["output_id"] == row.id
    ds = session.get(Dataset, d["id"])
    run, job = enqueue_run(session, "seg", ds, model=model)
    session.commit()
    jobs.run_pending()  # a cache hit: no model call
    session.expire_all()
    run = session.get(Run, run.id)
    assert run.status == "done", run.error
    assert next(iter(run.outputs.values()))["cache_id"] == row.id
    assert run.builds == {"pyannote-diarization": {"build_key": SAMPLE_BUILD,
                                                   "image": "ghcr.io/owner/pyannote-diarization:0.1.0"}}
    assert any("1 from cache" in line for line in jobs.read_log(job.id))
    assert session.scalar(select(Score).where(Score.run_id == run.id)).aggregates["der"] is not None


def test_outputs_from_another_build_or_unknown_params_are_skipped(replay, client, session, tmp_path):
    _catalog_with_seg(session)
    session.commit()
    pkg = _copy_complete(tmp_path)
    rec = json.loads((pkg / "outputs" / "seg.jsonl").read_text())
    no_build = {k: v for k, v in rec.items() if k != "build_key"}
    lines = [{**rec, "build_key": "0123456789abcdef"},
             {**no_build, "params": {**rec["params"], "image": rec["image"]}},  # a package from before the catalog
             {**rec, "params": {"sample_rate": 16000, "num_speakers": 2}},
             {**rec, "model": "kokoro-tts"}, {"model": "x"}]
    (pkg / "outputs" / "seg.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")
    import_id = stage(client, pkg)["id"]
    accept(client, import_id)
    out = client.get(f"/api/reference-imports/{import_id}").json()["result"]["outputs"]
    assert out["imported"] == 0 and out["lines"] == 6
    assert out["skipped"] == {"build_differs": 1, "no_build_key": 1, "params_differ": 1, "unknown_model": 1,
                              "invalid": 2, "not_in_dataset": 0}
    assert session.scalar(select(ModelOutput)) is None


def test_params_with_an_old_image_key_still_match(replay, client, session, tmp_path):
    """`params.image` from writers before the catalog is ignored: the build is matched on build_key."""
    _catalog_with_seg(session)
    session.commit()
    pkg = _copy_complete(tmp_path)
    rec = json.loads((pkg / "outputs" / "seg.jsonl").read_text())
    rec["params"]["image"] = rec["image"]
    (pkg / "outputs" / "seg.jsonl").write_text(json.dumps(rec) + "\n")
    import_id = stage(client, pkg)["id"]
    accept(client, import_id)
    assert client.get(f"/api/reference-imports/{import_id}").json()["result"]["outputs"]["imported"] == 1


# ---------------------------------------------------------------- flags back to Label


def test_flags_file_passes_the_contract_checker(client, session, tmp_path):
    d = import_complete(client)
    segs = segments_of(session, d["id"])
    client.post(f"/api/segments/{segs[1].id}/flag", json={"flagged": True, "note": "'parcel' is 'package'"})
    # a segment the package had no uid for
    segs[2].meta = {}
    session.commit()
    client.post(f"/api/segments/{segs[2].id}/flag", json={"flagged": True})
    r = client.get(f"/api/datasets/{d['id']}/flags.json")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    path = tmp_path / "flags.json"
    path.write_bytes(r.content)
    assert _checker().check_flags(path) == []
    doc = r.json()
    sample = json.loads((SAMPLES / "flags.json").read_text())
    assert set(doc) == set(sample) and doc["format_version"] == 2 and doc["dataset_name"] == "Help desk calls"
    assert (doc["catalog_dataset_id"], doc["catalog_version"]) == (sample["catalog_dataset_id"], 1)
    assert doc["created_at"].endswith("Z")
    assert [(f["uid"], f["file_sha256"], f["start"], f["end"], f["note"]) for f in doc["flags"]] == [
        (LABEL_UIDS[1], CLIP_SHA, 2.9, 6.81, "'parcel' is 'package'"), (None, CLIP_SHA, 7.11, 9.96, None)]
    # Label is not linked: the push is refused with a reason, the download still works
    r = client.post(f"/api/datasets/{d['id']}/flags/send")
    assert r.status_code == 409 and "LABEL_URL is not set" in r.json()["detail"]


def test_flags_pushed_to_a_reachable_label(client, session, monkeypatch):
    from concord_bench import peer

    d = import_complete(client)
    seg = segments_of(session, d["id"])[0]
    client.post(f"/api/segments/{seg.id}/flag", json={"flagged": True, "note": "wrong"})
    fake_label(monkeypatch)
    sent = {}

    def post(url, json, timeout):
        sent["url"], sent["doc"] = url, json
        return FakeResponse(200, _sample("label.flags-import.response.json"))

    monkeypatch.setattr(peer.httpx, "post", post)
    r = client.post(f"/api/datasets/{d['id']}/flags/send")
    assert r.json() == {"matched": 2, "unmatched": 0, "sent": 1}
    assert sent["url"] == "http://label.test:8610/api/flags/import" and sent["doc"]["flags"][0]["uid"] == LABEL_UIDS[0]
    monkeypatch.setattr(peer.httpx, "post", lambda *a, **k: FakeResponse(404, {"detail": "no dataset"}))
    assert "has no dataset" in client.post(f"/api/datasets/{d['id']}/flags/send").json()["detail"]


# ---------------------------------------------------------------- endpoints Label calls


def test_push_endpoints_are_out_of_the_contract(client):
    """Contract version 3 dropped the push from Label; the uploads stay for Bench's own pages."""
    assert client.post("/api/fingerprints/missing", json={"sha256": [CLIP_SHA]}).status_code in (404, 405)
    assert client.post("/api/uploads").status_code == 200


def fixture_package(dest: Path, version: int = 1, catalog_id: str | None = None) -> Path:
    """A reference package of the six usable fixture dialogues with the STM references the recorded
    responses were taken with (so ASR runs replay). With `catalog_id`, a version of that Dataset Catalog dataset."""
    t = truth()
    audio = GEN / "dataset" / "audio"
    (dest / "audio").mkdir(parents=True)
    (dest / "refs").mkdir()
    rows = []
    for p in sorted(audio.iterdir()):
        if p.stem in ("d07_corrupt", "d08_long"):
            continue
        shutil.copyfile(p, dest / "audio" / p.name)
        rows.append([p.name, hashlib.sha256(p.read_bytes()).hexdigest(), t[p.stem]["duration"], 16000])
    with open(dest / "files.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "sha256", "duration", "sample_rate"])
        w.writerows(rows)
    shutil.copyfile(GEN / "refs" / "stm" / "fixtures.stm", dest / "refs" / "fixtures.stm")
    (dest / "manifest.json").write_text(json.dumps({
        "format": "concord-reference-package", "format_version": 1, "name": "Synth calls",
        "created_at": "2026-10-22T09:00:00Z", "language": "en",
        "source": {"app": "concord-label", "install_id": "dataset-catalog" if catalog_id else "inst1",
                   "dataset_uid": catalog_id or "uid-synth", "dataset_name": "Synth calls"},
        "export_version": version, "scope": "complete", "label_types": ["asr", "diar"],
        "counts": {"files": len(rows), "segments": 0, "hours": 0.01}, "audio_included": "all"}))
    return dest


def test_scores_by_catalog(replay, client, session, tmp_path, monkeypatch):
    cid = "c0ffee" + "0" * 26
    assert client.get(f"/api/datasets/by-catalog/{cid}/scores").status_code == 404
    d = accept(client, stage(client, fixture_package(tmp_path / "pkg", catalog_id=cid))["id"])
    assert d["catalog"]["dataset_id"] == cid and d["catalog"]["n"] == 1
    r = client.post("/api/benchmarks/plan", json={"dataset_id": d["id"], "asr_models": ["whisper-large-v3"],
                                                  "mode": "stages"})
    assert r.json()["queued"] == 1
    jobs.run_pending()
    body = client.get(f"/api/datasets/by-catalog/{cid}/scores?normaliser=basic").json()
    sample = _sample("bench.scores-by-catalog.response.json")
    assert set(body) == set(sample) and set(body["dataset"]) == set(sample["dataset"])
    assert body["dataset"] == {"id": d["id"], "name": "Synth calls", "catalog_version": 1}
    (s,) = body["scores"]
    assert set(s) == set(sample["scores"][0])
    assert s["model"] == "whisper-large-v3" and s["normaliser"].startswith("basic@") and 0 <= s["wer"] < 0.5
    assert s["created_at"].endswith("Z")
    assert client.get(f"/api/datasets/by-catalog/{'f' * 32}/scores").status_code == 404
    # no score with that normaliser: the latest score
    assert client.get(f"/api/datasets/by-catalog/{cid}/scores?normaliser=nope").json()["scores"][0]["score_id"] \
        == s["score_id"]
    # a package from Label's own install (contract version 2) has no catalog dataset: no scores, no links
    old = accept(client, stage(client, fixture_package(tmp_path / "old"))["id"])
    assert old["catalog"] is None
    # Open in Label on the ASR error page, only once Label answers with the same contract version
    utt = client.get(f"/api/errors/asr/utterances?score_id={s['score_id']}&limit=1").json()["rows"][0]
    assert client.get(f"/api/errors/asr/utterances/{utt['id']}").json()["label_url"] is None
    fake_label(monkeypatch)
    detail = client.get(f"/api/errors/asr/utterances/{utt['id']}").json()
    sha = session.get(AudioFile, utt["file_id"]).sha256
    assert detail["label_url"] == (f"http://label.test:8610/label/file/by-sha/{sha}?catalog_dataset_id={cid}"
                                   f"&t={round(utt['start'], 3)}")
    old_file = session.scalar(select(AudioFile).where(AudioFile.dataset_id == old["id"]))
    assert peer_link(session, old_file.id) is None


def peer_link(session, audio_file_id: int) -> str | None:
    from concord_bench import peer

    return peer.file_link_for(session, audio_file_id)


# ---------------------------------------------------------------- the peer in /api/config


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code, self._body, self.text = status_code, body, json.dumps(body)

    def json(self):
        return self._body


def fake_label(monkeypatch, contract_version: int = CONTRACT_VERSION) -> list[str]:
    """LABEL_URL set and Label answering /api/config (counted)."""
    from concord_bench import peer
    from concord_bench.core.settings import reset_settings

    calls: list[str] = []

    def get(url, timeout):
        calls.append(url)
        return FakeResponse(200, {**_sample("config.label.json"), "contract_version": contract_version})

    monkeypatch.setenv("LABEL_URL", "http://label.test:8610/")
    monkeypatch.setattr(peer.httpx, "get", get)
    reset_settings()
    peer.clear_cache()
    return calls


def test_config_peer_unset_unreachable_and_reachable(client, monkeypatch):
    from concord_bench import peer
    from concord_bench.core.settings import reset_settings

    assert client.get("/api/config").json()["peer"] == {"url": None, "reachable": False, "contract_version": None}
    monkeypatch.setenv("LABEL_URL", "http://127.0.0.1:9")
    reset_settings()
    peer.clear_cache()
    assert client.get("/api/config").json()["peer"] == {"url": "http://127.0.0.1:9", "reachable": False,
                                                        "contract_version": None}
    calls = fake_label(monkeypatch)
    for _ in range(3):
        assert client.get("/api/config").json()["peer"] == {"url": "http://label.test:8610", "reachable": True,
                                                            "contract_version": CONTRACT_VERSION}
    assert calls == ["http://label.test:8610/api/config"]  # cached between page loads
    # an app that is not Label is not a peer
    monkeypatch.setattr(peer.httpx, "get", lambda url, timeout: FakeResponse(200, _sample("config.bench.json")))
    peer.clear_cache()
    assert client.get("/api/config").json()["peer"]["reachable"] is False


def test_open_in_label_needs_the_same_contract_version(client, session, monkeypatch):
    from concord_bench import peer

    d = import_complete(client)
    f = session.scalar(select(AudioFile).where(AudioFile.dataset_id == d["id"]))
    fake_label(monkeypatch, contract_version=CONTRACT_VERSION - 1)
    assert peer.file_link_for(session, f.id, 2.9) is None
    fake_label(monkeypatch)
    assert peer.file_link_for(session, f.id, 2.9) == (
        f"http://label.test:8610/label/file/by-sha/{CLIP_SHA}?catalog_dataset_id=8d4e2b6a1c9f4e3a9b7c5d1e2f3a4b5c"
        "&t=2.9")
