"""The Dataset Catalog in Bench (DATASET_CATALOG_PLAN.md §9, §12), against aicore's FakeDatasetCatalog:
originals in the store with holds, releasing holds when a dataset is deleted, and Bench without a store
behaving as before."""

from __future__ import annotations

import shutil

import pytest
from sqlalchemy import select

from concord_bench.core import paths
from concord_bench.core.models import AudioFile, Dataset
from concord_bench.datasets.ingest import ingest
from concord_bench.runs import jobs
from conftest import GEN, install_dataset_catalog


def fixture_source(tmp_path, name: str = "src"):
    src = tmp_path / name
    shutil.copytree(GEN / "dataset" / "audio", src / "audio")
    shutil.copytree(GEN / "refs" / "stm", src / "stm")
    return src


def ingest_fixture(session, tmp_path, name: str = "fx") -> Dataset:
    ds = Dataset(name=name)
    session.add(ds)
    session.flush()
    ingest(session, ds.id, fixture_source(tmp_path, f"src-{name}"))
    session.commit()
    return ds


def shas_of(session, ds: Dataset) -> set[str]:
    return set(session.scalars(select(AudioFile.sha256).where(AudioFile.dataset_id == ds.id)))


# ---------------------------------------------------------------- audio at upload (§9.1)


def test_config_says_whether_the_dataset_catalog_is_set_up(client, data_dir, monkeypatch):
    assert client.get("/api/config").json()["dataset_catalog"] == {
        "enabled": False, "ui_url": "http://127.0.0.1:8700/datasets"}
    from concord_bench.core.settings import reset_settings

    monkeypatch.setenv("DATASET_STORE", str(data_dir.parent / "store"))
    monkeypatch.setenv("CATALOG_UI_URL", "http://catalog.test:8700/")
    reset_settings()
    assert client.get("/api/config").json()["dataset_catalog"] == {
        "enabled": True, "ui_url": "http://catalog.test:8700/datasets"}


def test_without_a_store_originals_stay_in_concord_data(session, tmp_path, data_dir):
    ds = ingest_fixture(session, tmp_path)
    for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)):
        assert paths.original(f.sha256, f.ext) == data_dir / "originals" / f"{f.sha256}{f.ext}"
        assert paths.original(f.sha256, f.ext).is_file()


def test_ingest_uploads_originals_to_the_store_and_holds_them(session, tmp_path, data_dir, dataset_store):
    ds = ingest_fixture(session, tmp_path)
    files = list(session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)))
    assert len(files) == 7 and ds.status == "ready"
    for f in files:
        p = paths.original(f.sha256, f.ext)
        assert p == dataset_store.audio_path(f.sha256) and p.is_file()  # no extension in the store
        assert paths.derived(f.sha256).is_relative_to(data_dir)
        assert paths.derived(f.sha256).is_file() or f.status == "undecodable"
    assert not (data_dir / "originals").exists() or not any((data_dir / "originals").iterdir())
    assert dataset_store.held() == shas_of(session, ds)
    uploads = [c for c in dataset_store.calls if c[0] == "upload_audio"]
    assert len(uploads) == 7
    # the same audio again: nothing is sent twice
    ingest_fixture(session, tmp_path, "again")
    assert len([c for c in dataset_store.calls if c[0] == "upload_audio"]) == 7


def test_original_falls_back_to_concord_data_for_files_not_in_the_store(session, tmp_path, data_dir, monkeypatch):
    ds = ingest_fixture(session, tmp_path)  # before the store
    from concord_bench.core.settings import reset_settings

    monkeypatch.setenv("DATASET_STORE", str(tmp_path / "store"))
    reset_settings()
    f = session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)).first()
    assert paths.original(f.sha256, f.ext) == data_dir / "originals" / f"{f.sha256}{f.ext}"


def test_playing_an_original_from_the_store_keeps_its_type(client, session, tmp_path, dataset_store):
    ds = ingest_fixture(session, tmp_path)
    f = session.scalar(select(AudioFile).where(AudioFile.dataset_id == ds.id, AudioFile.ext == ".wav"))
    r = client.get(f"/api/audio/{f.id}?original=true")
    assert r.status_code == 200 and r.headers["content-type"] in ("audio/x-wav", "audio/wav")
    assert r.content == dataset_store.audio_path(f.sha256).read_bytes()


def test_an_ingest_fails_when_the_catalog_does_not_answer(client, session, tmp_path, dataset_store, monkeypatch):
    import httpx

    def down(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(dataset_store, "upload_audio", down)
    uid = client.post("/api/uploads").json()["upload_id"]
    shutil.copytree(fixture_source(tmp_path), paths.uploads(uid), dirs_exist_ok=True)
    r = client.post("/api/datasets", json={"name": "fx", "upload_id": uid})
    assert r.status_code == 201, r.text
    jobs.run_pending()
    d = client.get(f"/api/datasets/{r.json()['dataset']['id']}").json()
    assert d["status"] == "failed" and "Dataset Catalog" in d["ingest_job"]["error"]


# ---------------------------------------------------------------- deleting releases holds


def test_deleting_a_dataset_releases_holds_no_other_dataset_needs(client, session, tmp_path, dataset_store):
    a = ingest_fixture(session, tmp_path, "a")
    b = ingest_fixture(session, tmp_path, "b")
    shared = shas_of(session, a)
    assert shared == shas_of(session, b) and dataset_store.held() == shared
    assert client.delete(f"/api/datasets/{a.id}").status_code == 204
    assert dataset_store.held() == shared  # b still has every file
    assert client.delete(f"/api/datasets/{b.id}").status_code == 204
    assert dataset_store.held() == set()
    assert all(dataset_store.audio(s) for s in shared)  # releasing never deletes audio


def test_a_catalog_that_does_not_answer_keeps_the_dataset(client, session, tmp_path, dataset_store, monkeypatch):
    import httpx

    ds = ingest_fixture(session, tmp_path)

    def down(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(dataset_store, "release", down)
    r = client.delete(f"/api/datasets/{ds.id}")
    assert r.status_code == 503 and "try again" in r.json()["detail"]
    assert client.get(f"/api/datasets/{ds.id}").status_code == 200
    assert dataset_store.held() == shas_of(session, ds)


@pytest.mark.parametrize("value", ["", "  "])
def test_an_empty_dataset_store_is_unset(monkeypatch, data_dir, value):
    from concord_bench.core.settings import get_settings, reset_settings

    monkeypatch.setenv("DATASET_STORE", value)
    reset_settings()
    assert get_settings().dataset_store is None and paths.store() is None


def test_a_dataset_store_under_home_is_expanded(monkeypatch, data_dir):
    from pathlib import Path

    from concord_bench.core.settings import get_settings, reset_settings

    monkeypatch.setenv("DATASET_STORE", "~/ai-data/datasets")
    reset_settings()
    assert get_settings().dataset_store == Path.home() / "ai-data" / "datasets"


# ---------------------------------------------------------------- just move-originals (§12)


def test_move_originals_uploads_holds_and_renames_the_folder(session, tmp_path, data_dir, monkeypatch):
    import hashlib

    from concord_bench.datasets.store import move_originals

    ds = ingest_fixture(session, tmp_path)  # before the store
    orphan = b"RIFF-not-used-by-any-dataset"
    orphan_sha = hashlib.sha256(orphan).hexdigest()
    (data_dir / "originals" / f"{orphan_sha}.wav").write_bytes(orphan)
    (data_dir / "originals" / "notes.txt").write_text("not an original")
    before = sorted(p.name for p in (data_dir / "originals").iterdir())
    fake = install_dataset_catalog(monkeypatch, tmp_path / "store")

    out = move_originals(session, log=lambda m: None)
    assert out["files"] == 9 and out["uploaded"] == 8 and out["already_in_store"] == 0
    assert out["held"] == 7 and out["not_used"] == 1 and out["skipped"] == ["notes.txt"] and out["failed"] == []
    assert fake.held() == shas_of(session, ds)  # the orphan is uploaded but not held
    assert fake.audio(orphan_sha) is not None
    assert not (data_dir / "originals").exists()
    assert sorted(p.name for p in (data_dir / "originals.moved").iterdir()) == before  # nothing deleted
    for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)):
        assert paths.original(f.sha256, f.ext) == fake.audio_path(f.sha256)

    # again: nothing left to move, and originals.moved is kept as it is
    again = move_originals(session, log=lambda m: None)
    assert again["files"] == 0 and again["moved_to"] is None
    assert sorted(p.name for p in (data_dir / "originals.moved").iterdir()) == before


def test_move_originals_keeps_the_folder_when_a_file_is_refused(session, tmp_path, data_dir, monkeypatch):
    from concord_bench.datasets.store import move_originals

    ingest_fixture(session, tmp_path)
    (data_dir / "originals" / f"{'ab' * 32}.wav").write_bytes(b"bytes that hash to something else")
    fake = install_dataset_catalog(monkeypatch, tmp_path / "store")
    out = move_originals(session, log=lambda m: None)
    assert len(out["failed"]) == 1 and out["failed"][0].startswith("abab") and out["moved_to"] is None
    assert (data_dir / "originals").is_dir() and not (data_dir / "originals.moved").exists()
    assert len(fake.held()) == 7  # what was stored is held all the same


def test_move_originals_needs_the_store(data_dir, capsys):
    from concord_bench import cli

    assert cli.main(["move-originals"]) == 2
    assert "DATASET_STORE" in capsys.readouterr().err
