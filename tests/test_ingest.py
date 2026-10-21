"""Ingest on the fixture set: counts, sample rates, flags, derived audio, references (M2)."""

from __future__ import annotations

import shutil
import zipfile

import soundfile as sf
from sqlalchemy import select

from concord_bench.core import paths
from concord_bench.core.models import AudioFile, Dataset, FileStatus, Segment
from concord_bench.datasets.ingest import ingest, inspect_source
from conftest import GEN, truth


def stage(tmp_path, refs: list[str]):
    src = tmp_path / "src"
    shutil.copytree(GEN / "dataset" / "audio", src / "audio")
    for r in refs:
        shutil.copytree(GEN / "refs" / r, src / r)
    return src


def test_inspect_matches_and_previews(tmp_path):
    rep = inspect_source(stage(tmp_path, ["stm"]))
    assert rep["audio_files"] == 8
    assert rep["tasks"]["spk_asr"]["files"] == 8
    assert len(rep["tasks"]["spk_asr"]["preview"]) == 3
    assert rep["unmatched"] == [] and rep["audio_without_labels"] == []


def test_inspect_nemo_diarisation_manifest(tmp_path):
    rep = inspect_source(stage(tmp_path, ["nemo-manifest"]))
    assert rep["tasks"]["diar"]["files"] == 8 and rep["parse_errors"] == []
    shutil.rmtree(tmp_path / "src" / "nemo-manifest" / "rttm")
    rep = inspect_source(tmp_path / "src")
    assert "diar" not in rep["tasks"]
    [err] = rep["parse_errors"]
    assert err["format"] == "nemo-manifest" and err["count"] == 8 and "d01.rttm is not in the upload" in err["errors"][0]


def test_inspect_mixed_formats_and_unmatched(tmp_path):
    src = stage(tmp_path, ["rttm", "asr-txt"])
    (src / "asr-txt" / "zz_missing.txt").write_text("nothing")  # .txt with no audio: ignored, not unmatched
    (src / "extra.jsonl").write_text('{"audio_filepath": "nope.wav", "text": "x"}\n')
    rep = inspect_source(src)
    assert set(rep["tasks"]) == {"diar", "asr"}
    assert rep["unmatched"] == ["nope"]


def test_ingest_fixture_dataset(session, tmp_path):
    src = stage(tmp_path, ["stm"])
    ds = Dataset(name="fx")
    session.add(ds)
    session.flush()
    rep = ingest(session, ds.id, src)
    session.commit()
    assert (rep["files"], rep["usable_files"]) == (8, 6)
    assert rep["sample_rates"] == {"8000": 1, "16000": 5, "44100": 1}
    assert [f["file"] for f in rep["flagged"]["clipped"]] == ["audio/d05_clipped.wav"]
    assert [f["file"] for f in rep["flagged"]["undecodable"]] == ["audio/d07_corrupt.wav"]
    assert [f["file"] for f in rep["flagged"]["too_long"]] == ["audio/d08_long.mp3"]
    assert ds.label_types == ["asr", "diar"] and rep["has_speaker_text"]
    files = {f.stem: f for f in session.scalars(select(AudioFile))}
    t = truth()
    for stem in ("d01", "d02_8k", "d03_44k_stereo", "d04", "d06"):
        f = files[stem]
        assert abs(f.duration - t[stem]["duration"]) < 0.08, stem  # mp3 / ogg encoder padding
        data, sr = sf.read(paths.derived(f.sha256))
        assert sr == 16000 and data.ndim == 1
        assert paths.peaks(f.sha256).is_file()
        assert paths.original(f.sha256, f.ext).is_file()
    assert files["d03_44k_stereo"].channels == 2 and files["d03_44k_stereo"].orig_sr == 44100
    assert files["d07_corrupt"].status == FileStatus.undecodable
    segs = session.scalars(select(Segment).where(Segment.audio_file_id == files["d01"].id)).all()
    assert len(segs) == len(t["d01"]["turns"])
    assert files["d06"].meta == {"condition": "noisy"}


def test_ingest_file_level_text_and_zip(session, tmp_path):
    src = tmp_path / "zipsrc"
    src.mkdir()
    with zipfile.ZipFile(src / "upload.zip", "w") as zf:
        for p in (GEN / "dataset" / "audio").iterdir():
            if p.stem in ("d01", "d04"):
                zf.write(p, f"audio/{p.name}")
        zf.write(GEN / "refs" / "asr-jsonl" / "manifest.jsonl", "manifest.jsonl")
    ds = Dataset(name="zip")
    session.add(ds)
    session.flush()
    rep = ingest(session, ds.id, src)
    assert rep["files"] == 2 and ds.label_types == ["asr"]
    seg = session.scalars(select(Segment)).first()
    assert seg.speaker is None and seg.start == 0.0


def test_reingest_replaces_references(session, tmp_path):
    src = stage(tmp_path, ["stm"])
    ds = Dataset(name="fx")
    session.add(ds)
    session.flush()
    ingest(session, ds.id, src)
    n = session.query(Segment).count()
    ingest(session, ds.id, src)
    assert session.query(Segment).count() == n
