"""Dataset Catalog versions in Bench (DATASET_CATALOG_PLAN.md §9.2), against aicore's FakeDatasetCatalog:
importing a version from the store's mount, the chain of Bench datasets made from one catalog dataset, holds
and uses, and the exit check of D4: a version imported from the catalog scores the same as the same zip
imported as before."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from concord_bench.core import db, paths
from concord_bench.core.db import SessionLocal
from concord_bench.core.models import AudioFile, Dataset, Result, Run, Score, Segment
from concord_bench.runs import jobs
from conftest import install_dataset_catalog
from test_reference_packages import CLIP_SHA, LABEL_UIDS, SAMPLES, accept, fixture_package, upload_zip, zip_folder

HELP_DESK = SAMPLES / "reference-package-complete"


def stage_version(client, catalog_id: str, n: int) -> dict:
    r = client.post("/api/dataset-catalog/imports", json={"dataset_id": catalog_id, "n": n})
    assert r.status_code == 201, r.text
    return r.json()


def help_desk(fake, tmp_path) -> str:
    """A catalog dataset whose v1 is the contract's complete sample package (audio, uids, labels)."""
    did = fake.create_dataset("Help desk calls", language="en")["id"]
    v = fake.publish(did, HELP_DESK)
    assert v["created"] and v["n"] == 1
    return did


# ---------------------------------------------------------------- listing


def test_the_catalog_needs_the_store(client):
    r = client.get("/api/dataset-catalog/datasets")
    assert r.status_code == 409 and "DATASET_STORE" in r.json()["detail"]
    assert client.post("/api/dataset-catalog/imports", json={"dataset_id": "x", "n": 1}).status_code == 409


def test_datasets_and_versions_say_which_can_be_imported(client, dataset_store, tmp_path):
    did = help_desk(dataset_store, tmp_path)
    audio_only = dataset_store.create_dataset("Raw calls")["id"]
    dataset_store.add_version(audio_only, {"a.wav": (HELP_DESK / "audio" / "check_clip.wav").read_bytes()},
                              label_types=())
    listed = {d["name"]: d for d in client.get("/api/dataset-catalog/datasets").json()}
    assert listed["Help desk calls"]["latest"]["importable"] is True
    assert listed["Raw calls"]["latest"]["importable"] is False and listed["Raw calls"]["latest"]["label_types"] == []
    assert [d["name"] for d in client.get("/api/dataset-catalog/datasets?q=raw").json()] == ["Raw calls"]
    d = client.get(f"/api/dataset-catalog/datasets/{did}").json()
    assert d["url"] == f"http://127.0.0.1:8700/datasets/{did}"
    (v1,) = d["versions"]
    assert v1["n"] == 1 and v1["importable"] and v1["bench"] == [] and v1["counts"]["files"] == 1
    assert client.get("/api/dataset-catalog/datasets/nope").status_code == 404
    # an audio-only version is listed but not imported (the NO_REFERENCES rule)
    r = client.post("/api/dataset-catalog/imports", json={"dataset_id": audio_only, "n": 1})
    assert r.status_code == 400 and "audio only" in r.json()["detail"]


def test_a_catalog_that_does_not_answer_is_503(client, dataset_store, monkeypatch):
    import httpx

    def down(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(dataset_store, "datasets", down)
    r = client.get("/api/dataset-catalog/datasets")
    assert r.status_code == 503 and "did not answer" in r.json()["detail"]


# ---------------------------------------------------------------- importing


def test_import_a_version_from_the_mount(client, session, dataset_store, tmp_path, data_dir):
    did = help_desk(dataset_store, tmp_path)
    folder = dataset_store.version_dir(did, 1)
    before = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))
    staged = stage_version(client, did, 1)
    p = staged["preview"]
    assert staged["status"] == "staged" and p["catalog"] == {"dataset_id": did, "n": 1, "dataset_name": "Help desk calls"}
    assert p["source"]["install_id"] == "dataset-catalog" and p["source"]["dataset_uid"] == did
    assert p["export_version"] == 1 and p["files"] == 1 and p["new_files"] == 0 and p["missing_files"] == []
    assert p["segments"] == 3 and p["uids"] == 3 and p["labels"] == ["channel", "condition", "speaker.accent.region",
                                                                      "speaker.gender"]
    assert stage_version(client, did, 1)["id"] == staged["id"]  # still staged: the same import

    d = accept(client, staged["id"])
    assert d["name"] == "Help desk calls" and d["label_types"] == ["asr", "diar"] and d["export_version"] == 1
    assert d["catalog"] == {"dataset_id": did, "n": 1, "published": False,
                            "url": f"http://127.0.0.1:8700/datasets/{did}/v/1"}
    assert d["from_catalog"] is True
    ds = session.get(Dataset, d["id"])
    assert ds.source_key == f"dataset-catalog/{did}"
    (f,) = session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id))
    assert paths.original(f.sha256, f.ext) == dataset_store.audio_path(f.sha256)
    assert not (data_dir / "originals").exists() or not any((data_dir / "originals").iterdir())
    segs = list(session.scalars(select(Segment).where(Segment.audio_file_id == f.id).order_by(Segment.start)))
    assert [s.uid for s in segs] == LABEL_UIDS  # refs/uids.jsonl
    assert f.meta == {"condition": "clean", "channel": "phone"}
    # holds and a use with Bench's dataset id
    assert dataset_store.held() == {f.sha256}
    assert dataset_store.version(did, 1)["uses"][0]["ref"] == str(ds.id)
    imp = client.get(f"/api/reference-imports/{staged['id']}").json()
    assert imp["status"] == "accepted" and imp["result"]["use"] is True and imp["result"]["uids"]["matched"] == 3
    # the version folder on the mount is read, never changed
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) == before
    v = client.get(f"/api/dataset-catalog/datasets/{did}").json()["versions"][0]
    assert v["bench"] == [{"id": ds.id, "name": "Help desk calls", "status": "ready", "published": False}]

    # deleting the Bench dataset takes it off the version's uses and releases its holds
    assert client.delete(f"/api/datasets/{ds.id}").status_code == 204
    assert dataset_store.version(did, 1)["uses"] == [] and dataset_store.held() == set()


def test_the_next_version_is_linked_to_the_dataset_made_from_the_previous_one(client, dataset_store, tmp_path):
    did = help_desk(dataset_store, tmp_path)
    v1 = accept(client, stage_version(client, did, 1)["id"])
    any_ = SAMPLES / "reference-package-any"  # same audio, two segments without speakers
    v = dataset_store.publish(did, any_, parent=1)
    assert v["n"] == 2
    staged = stage_version(client, did, 2)
    p = staged["preview"]
    assert p["previous_version"] == {"dataset_id": v1["id"], "name": "Help desk calls", "export_version": 1}
    assert p["dataset_name"] == "Help desk calls v2"
    v2 = accept(client, staged["id"])
    assert v2["previous_version_id"] == v1["id"] and v2["label_types"] == ["asr"]
    assert [(x["id"], x["export_version"]) for x in v2["versions"]] == [(v1["id"], 1), (v2["id"], 2)]
    assert v2["previous_plan"]["previous"]["id"] == v1["id"]


def test_a_version_whose_folder_is_not_on_the_mount(client, dataset_store, tmp_path):
    did = help_desk(dataset_store, tmp_path)
    shutil.rmtree(dataset_store.version_dir(did, 1))
    r = client.post("/api/dataset-catalog/imports", json={"dataset_id": did, "n": 1})
    assert r.status_code == 400 and "DATASET_STORE" in r.json()["detail"]


# ---------------------------------------------------------------- the exit check: zip and catalog score the same


def _facts(s, dataset_id: int) -> dict:
    """Everything an import puts in a dataset and everything its runs score, without ids and times."""
    ds = s.get(Dataset, dataset_id)
    files = list(s.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id).order_by(AudioFile.rel_path)))
    segs = {f.rel_path: [(x.start, x.end, x.speaker, x.text, x.status, x.uid if (x.meta or {}).get("source_uid")
                          else None, dict(x.meta or {}))
                         for x in sorted(f.segments, key=lambda x: (x.start, x.end, x.speaker or ""))] for f in files}
    scores = {}
    for run in s.scalars(select(Run).where(Run.dataset_id == ds.id).order_by(Run.id)):
        key = (run.task, run.model.catalog_id if run.model else None,
               (run.pipeline.seg_model.catalog_id if run.pipeline and run.pipeline.seg_model else None)
               if run.pipeline else None)
        assert run.status == "done", (key, run.error)
        for sc in s.scalars(select(Score).where(Score.run_id == run.id)):
            rows = s.scalars(select(Result).where(Result.score_id == sc.id)
                             .order_by(Result.utterance_key, Result.start)).all()
            scores[(*key, sc.normaliser, sc.collar, sc.skip_overlap)] = (
                sc.aggregates, [(r.audio_file.rel_path, r.utterance_key.split(":", 1)[-1], r.start, r.end,
                                 r.speaker, r.ref_text, r.hyp_text, r.wer, r.errors, r.metrics) for r in rows])
    return {"name": ds.name, "language": ds.language, "label_types": ds.label_types, "status": ds.status,
            "files": [(f.rel_path, f.sha256, f.ext, f.duration, f.orig_sr, f.channels, f.status, f.meta)
                      for f in files], "segments": segs, "report": ds.report, "scores": scores}


def _score(client, dataset_id: int) -> None:
    for plan in ({"asr_models": ["whisper-large-v3"], "seg_models": ["pyannote-diarization"], "mode": "stages"},
                 {"asr_models": ["whisper-large-v3"], "oracle": True, "mode": "grid"}):
        r = client.post("/api/benchmarks/plan", json={"dataset_id": dataset_id, **plan})
        assert r.status_code == 202 and r.json()["queued"] >= 1, r.text
        jobs.run_pending()


def test_a_catalog_version_scores_the_same_as_the_same_zip(replay, client, tmp_path, monkeypatch):
    """The fixture dialogues as a reference package zip, imported as before (no store), and the same package
    published to the Dataset Catalog and imported from there, each in a Bench of its own: the datasets and
    their scores on replayed model runs (ASR, diarisation and an oracle pipeline) are identical."""
    pkg = fixture_package(tmp_path / "pkg")
    staged = client.post("/api/reference-imports", json={"upload_id": upload_zip(client, zip_folder(pkg))}).json()
    by_zip = accept(client, staged["id"])
    _score(client, by_zip["id"])
    with SessionLocal() as s:
        zip_facts = _facts(s, by_zip["id"])

    # a second Bench, with the store
    monkeypatch.setenv("CONCORD_DATA", str(tmp_path / "data-b"))
    fake = install_dataset_catalog(monkeypatch, tmp_path / "store")
    db.reset_engines()
    db.migrate()
    did = fake.create_dataset("Synth calls", language="en")["id"]
    assert fake.publish(did, pkg)["n"] == 1  # the same package, as the catalog's Import package does
    by_catalog = accept(client, stage_version(client, did, 1)["id"])
    _score(client, by_catalog["id"])
    with SessionLocal() as s:
        catalog_facts = _facts(s, by_catalog["id"])
        shas = set(s.scalars(select(AudioFile.sha256)))
    assert not (tmp_path / "data-b" / "originals").exists() and fake.held() == shas

    assert zip_facts["scores"] and len(zip_facts["scores"]) >= 3
    assert catalog_facts == zip_facts
    db.reset_engines()


def test_a_format_version_3_version_with_references_imports(client, dataset_store):
    did = dataset_store.create_dataset("Calls")["id"]
    refs = (HELP_DESK / "refs" / "segments.seglst.json").read_text()
    dataset_store.add_version(did, {"check_clip.wav": (HELP_DESK / "audio" / "check_clip.wav").read_bytes()},
                              {"segments.seglst.json": refs}, label_types=("asr", "diar"),
                              format_version=3)
    d = accept(client, stage_version(client, did, 1)["id"])
    assert d["label_types"] == ["asr", "diar"] and d["has_speaker_text"]


def label_download(tmp_path) -> Path:
    """The sample package as Label builds it for download: its source is Label's own install, not a catalog
    version."""
    pkg = tmp_path / "label-download"
    shutil.copytree(HELP_DESK, pkg)
    m = json.loads((pkg / "manifest.json").read_text())
    (pkg / "manifest.json").write_text(json.dumps({**m, "source": {**m["source"], "install_id": "3f2a9c1e5b7d4a60"}}))
    return pkg


def test_a_zip_still_imports_with_the_store_set_up(client, dataset_store, tmp_path):
    staged = client.post("/api/reference-imports",
                         json={"upload_id": upload_zip(client, zip_folder(label_download(tmp_path)))})
    assert staged.status_code == 201, staged.text
    d = accept(client, staged.json()["id"])
    assert d["catalog"] is None and d["from_catalog"] is False
    assert dataset_store.held() == {CLIP_SHA} and dataset_store.audio(CLIP_SHA) is not None


# ---------------------------------------------------------------- publishing from Bench (§9.3)


def ingest_refs(session, tmp_path, refs: str, name: str = "fx") -> Dataset:
    from concord_bench.datasets.ingest import ingest
    from conftest import GEN

    src = tmp_path / f"src-{name}"
    shutil.copytree(GEN / "dataset" / "audio", src / "audio")
    shutil.copytree(GEN / "refs" / refs, src / refs)
    ds = Dataset(name=name, language="en", tags=["synth"])
    session.add(ds)
    session.flush()
    ingest(session, ds.id, src)
    session.commit()
    return ds


def references(s, dataset_id: int) -> dict:
    """Files with their labels, and segments with their uids and labels, by path."""
    from concord_bench.datasets import labels

    s.expire_all()
    files = s.scalars(select(AudioFile).where(AudioFile.dataset_id == dataset_id).order_by(AudioFile.rel_path))
    return {f.rel_path: (f.sha256, f.meta, [(x.start, x.end, x.speaker, x.text, x.uid, labels.of(x.meta))
                                            for x in sorted(f.segments, key=lambda x: (x.start, x.end, x.speaker or ""))])
            for f in files if f.usable}


def publish(client, dataset_id: int, status: int = 201, **body) -> dict:
    r = client.post(f"/api/datasets/{dataset_id}/publish", json=body)
    assert r.status_code == status, r.text
    return r.json()


@pytest.mark.parametrize(("refs", "refs_file", "label_types"), [
    ("stm", "segments.seglst.json", ["asr", "diar"]),
    ("asr-jsonl", "segments.jsonl", ["asr"]),
    ("rttm", "refs.rttm", ["diar"]),
])
def test_publish_an_upload_as_v1_and_import_it_back_unchanged(client, session, dataset_store, tmp_path, refs,
                                                             refs_file, label_types):
    ds = ingest_refs(session, tmp_path, refs)
    out = publish(client, ds.id, name="Synth calls")
    v = out["version"]
    assert out["created"] and v["n"] == 1 and v["parent_n"] is None and v["label_types"] == label_types
    did = v["dataset_id"]
    assert out["dataset"]["catalog"] == {"dataset_id": did, "n": 1, "published": True,
                                         "url": f"http://127.0.0.1:8700/datasets/{did}/v/1"}
    cat = dataset_store.dataset(did)
    assert cat["name"] == "Synth calls" and cat["language"] == "en" and cat["tags"] == ["synth"]
    folder = dataset_store.version_dir(did, 1)
    assert sorted(p.name for p in (folder / "refs").iterdir()) == sorted([refs_file, "uids.jsonl"])
    assert not (folder / "audio").exists() and not (folder / "outputs").exists()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["format_version"] == 2 and manifest["counts"]["files"] == 6  # the undecodable file is left out
    assert dataset_store.version(did, 1)["uses"][0]["ref"] == str(ds.id)
    assert dataset_store.held() == {sha for sha, *_ in references(session, ds.id).values()} | {
        session.scalar(select(AudioFile.sha256).where(AudioFile.status == "undecodable"))}
    # the version imported again: the same references, uids and labels
    back = accept(client, stage_version(client, did, 1)["id"])
    assert back["label_types"] == label_types
    mine, theirs = references(session, ds.id), references(session, back["id"])
    assert theirs == mine
    v1 = client.get(f"/api/dataset-catalog/datasets/{did}").json()["versions"][0]
    assert [(b["id"], b["published"]) for b in v1["bench"]] == [(ds.id, True), (back["id"], False)]


def test_labels_and_uids_from_a_zip_survive_publishing(client, session, dataset_store, tmp_path):
    staged = client.post("/api/reference-imports",
                         json={"upload_id": upload_zip(client, zip_folder(label_download(tmp_path)))})
    d = accept(client, staged.json()["id"])
    did = publish(client, d["id"])["version"]["dataset_id"]
    folder = dataset_store.version_dir(did, 1)
    attrs = json.loads((folder / "refs" / "attributes.json").read_text())
    assert attrs == json.loads((HELP_DESK / "refs" / "attributes.json").read_text())
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["scope"] == "complete" and manifest["normaliser"] == "basic"
    back = accept(client, stage_version(client, did, 1)["id"])
    assert references(session, back["id"]) == references(session, d["id"])
    assert [x[4] for x in references(session, back["id"])["check_clip.wav"][2]] == LABEL_UIDS
    with SessionLocal() as s:
        assert s.get(Dataset, back["id"]).report["labels"] == s.get(Dataset, d["id"]).report["labels"]


def test_publish_a_new_version_of_an_existing_dataset(client, session, dataset_store, tmp_path):
    did = help_desk(dataset_store, tmp_path)
    ds = ingest_refs(session, tmp_path, "stm")
    # the person saw v1 as the latest, but another app published v2 meanwhile
    dataset_store.publish(did, SAMPLES / "reference-package-any", parent=1)
    r = client.post(f"/api/datasets/{ds.id}/publish", json={"catalog_dataset_id": did, "parent": 1})
    assert r.status_code == 409 and "latest version is now v2, published by Concord Bench" in r.json()["detail"]
    assert session.get(Dataset, ds.id).catalog_dataset_id is None
    out = publish(client, ds.id, catalog_dataset_id=did, parent=1, force=True)
    assert out["version"]["n"] == 3 and out["version"]["parent_n"] == 1
    assert dataset_store.version(did, 3)["published_by"] == "concord-bench"
    # published once: not again
    r = client.post(f"/api/datasets/{ds.id}/publish", json={})
    assert r.status_code == 400 and "already published as v3" in r.json()["detail"]


def test_without_a_parent_the_next_version_is_based_on_the_latest(client, session, dataset_store, tmp_path):
    did = help_desk(dataset_store, tmp_path)
    ds = ingest_refs(session, tmp_path, "rttm")
    assert publish(client, ds.id, catalog_dataset_id=did)["version"]["parent_n"] == 1


def test_datasets_made_from_catalog_versions_are_published_from_label(client, dataset_store, tmp_path):
    did = help_desk(dataset_store, tmp_path)
    d = accept(client, stage_version(client, did, 1)["id"])
    detail = client.get(f"/api/datasets/{d['id']}").json()
    assert "Concord Label" in detail["publish_blocker"]
    r = client.post(f"/api/datasets/{d['id']}/publish", json={})
    assert r.status_code == 400 and "Concord Label" in r.json()["detail"]
    # a zip downloaded from the catalog is a catalog version too
    pkg = tmp_path / "pkg"
    shutil.copytree(dataset_store.version_dir(did, 1), pkg)
    shutil.copytree(HELP_DESK / "audio", pkg / "audio")
    manifest = json.loads((pkg / "manifest.json").read_text())
    (pkg / "manifest.json").write_text(json.dumps({**manifest, "audio_included": "all"}))
    z = accept(client, client.post("/api/reference-imports", json={"upload_id": upload_zip(client, zip_folder(pkg))})
               .json()["id"])
    # it names that version, so flags and Label links work, but it isn't a use of the version
    assert z["from_catalog"] and (z["catalog"]["dataset_id"], z["catalog"]["n"]) == (did, 1)
    assert not z["catalog"]["published"]
    assert [u["ref"] for u in dataset_store.version(did, 1)["uses"]] == [str(d["id"])]
    assert client.post(f"/api/datasets/{z['id']}/publish", json={}).status_code == 400


def test_publish_uploads_originals_from_before_the_store(client, session, tmp_path, monkeypatch):
    ds = ingest_refs(session, tmp_path, "stm")  # originals in data/originals
    fake = install_dataset_catalog(monkeypatch, tmp_path / "store")
    assert not list((tmp_path / "store" / "audio").rglob("*"))
    did = publish(client, ds.id)["version"]["dataset_id"]
    assert fake.version(did, 1)["counts"]["files"] == 6
    assert len([c for c in fake.calls if c[0] == "upload_audio"]) == 6 and len(fake.held()) == 6


def test_a_failed_first_publish_leaves_no_empty_dataset(client, session, dataset_store, tmp_path, monkeypatch):
    from aicore.datasets import InvalidVersion

    ds = ingest_refs(session, tmp_path, "stm")

    def refuse(*a, **k):
        raise InvalidVersion(422, "the version is invalid: something", "invalid_version", problems=["something"])

    monkeypatch.setattr(dataset_store, "publish", refuse)
    r = client.post(f"/api/datasets/{ds.id}/publish", json={})
    assert r.status_code == 422 and "invalid" in r.json()["detail"]
    assert dataset_store.datasets() == [] and session.get(Dataset, ds.id).catalog_dataset_id is None
