"""Annotation parsers: every supported format gives the same truth; malformed files report errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concord.datasets.parsers import parse_file
from conftest import GEN, truth

REFS = GEN / "refs"


def records(fmt_dir: str):
    out = []
    for p in sorted((REFS / fmt_dir).iterdir()):
        res = parse_file(p)
        assert res.errors == [], (p, res.errors)
        out += res.records
    return out


def by_turn(recs):
    return sorted((r.stem, round(r.start, 3), round(r.end, 3), r.speaker, r.text) for r in recs)


def expected(with_text: bool = True):
    return sorted((stem, t["start"], t["end"], t["speaker"], t["text"] if with_text else None)
                  for stem, f in truth().items() for t in f["turns"])


@pytest.mark.parametrize("fmt,fmt_id", [("stm", "stm"), ("seglst", "seglst"), ("json-segments", "json-segments"),
                                        ("jsonl-segments", "json-segments")])
def test_speaker_attributed_formats(fmt, fmt_id):
    recs = records(fmt)
    assert by_turn(recs) == expected()
    assert {r.kind for r in recs} == {"spk_asr"}
    assert parse_file(next((REFS / fmt).iterdir())).format == fmt_id


def test_rttm_and_diar_json():
    assert by_turn(records("rttm")) == expected(with_text=False)
    assert by_turn(records("diar-json")) == expected(with_text=False)
    assert {r.kind for r in records("rttm")} == {"diar"}


@pytest.mark.parametrize("fmt", ["asr-jsonl", "asr-csv", "asr-txt"])
def test_file_level_asr_formats(fmt):
    recs = records(fmt)
    want = {stem: " ".join(t["text"] for t in f["turns"]) for stem, f in truth().items()}
    assert {r.stem: r.text for r in recs} == want
    assert all(r.start is None and r.speaker is None for r in recs)


def test_jsonl_extra_fields_become_metadata():
    recs = records("asr-jsonl")
    d01 = next(r for r in recs if r.stem == "d01")
    assert d01.meta == {"condition": "clean"}


def test_stm_condition_label():
    recs = records("stm")
    assert {r.meta.get("condition") for r in recs if r.stem == "d06"} == {"noisy"}


@pytest.mark.parametrize("name,n_errors,n_ok", [
    ("bad.rttm", 2, 1), ("bad.jsonl", 3, 1), ("bad.stm", 1, 1), ("bad.json", 1, 0), ("bad.csv", 1, 1)])
def test_malformed(name, n_errors, n_ok):
    res = parse_file(GEN / "malformed" / name)
    assert len(res.errors) == n_errors, res.errors
    assert len(res.records) == n_ok


def test_offset_duration_and_unknown_format(tmp_path: Path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"audio_filepath": "a/b/x.wav", "text": "hi", "offset": 1.5, "duration": 2}\n')
    r = parse_file(p).records[0]
    assert (r.stem, r.start, r.end) == ("x", 1.5, 3.5)
    q = tmp_path / "x.xml"
    q.write_text("<a/>")
    assert parse_file(q).format == "unknown"


def test_nemo_diarisation_output():
    recs = records("nemo-diar")
    assert by_turn(recs) == expected()
    assert {r.kind for r in recs} == {"spk_asr"}
    assert parse_file(next((REFS / "nemo-diar").iterdir())).format == "nemo-diar"


def test_nemo_diarisation_output_words_only(tmp_path: Path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"status": "success", "session_id": "call_01", "transcription": "hi there yes",
                             "speaker_count": 2, "words": [
                                 {"word": "hi", "start_time": 0.1, "end_time": 0.3, "speaker": "speaker_0"},
                                 {"word": "there", "start_time": 0.4, "end_time": 0.7, "speaker": "speaker_0"},
                                 {"word": "yes", "start_time": 1.0, "end_time": 1.2, "speaker": "speaker_1"}]}))
    res = parse_file(p)
    assert res.format == "nemo-diar" and res.errors == []
    assert by_turn(res.records) == [("call_01", 0.1, 0.7, "speaker_0", "hi there"),
                                    ("call_01", 1.0, 1.2, "speaker_1", "yes")]


def test_nemo_diarisation_manifest_points_at_rttms():
    res = parse_file(REFS / "nemo-manifest" / "manifest.json")
    assert res.format == "nemo-manifest" and res.errors == [] and res.records == []
    assert sorted(res.linked) == sorted(f"{stem}.rttm" for stem in truth())
    rttms = [r for p in sorted((REFS / "nemo-manifest" / "rttm").iterdir()) for r in parse_file(p).records]
    assert by_turn(rttms) == expected(with_text=False)


def test_nemo_asr_manifest_saved_as_json(tmp_path: Path):
    p = tmp_path / "train_manifest.json"
    p.write_text('{"audio_filepath": "/data/a.wav", "duration": 3.2, "text": "hello", "pred_text": "hallo"}\n'
                 '{"audio_filepath": "/data/b.wav", "offset": 1.0, "duration": 2.0, "text": "world", "lang": "en"}\n'
                 '{"audio_filepath": "/data/c.wav", "offset": 0, "duration": null, "text": "whole"}\n')
    res = parse_file(p)
    assert res.format == "nemo-manifest" and res.errors == []
    assert [(r.stem, r.start, r.end, r.text, r.meta) for r in res.records] == [
        ("a", None, None, "hello", {}), ("b", 1.0, 3.0, "world", {"lang": "en"}), ("c", None, None, "whole", {})]


def test_one_line_nemo_manifests():
    asr = parse_file(GEN / "templates" / "nemo_manifest.json")
    assert asr.format == "nemo-manifest" and [r.kind for r in asr.records] == ["asr"]
    diar = parse_file(GEN / "templates" / "nemo_diarisation_manifest.json")
    assert diar.format == "nemo-manifest" and diar.records == [] and diar.linked == ["diarisation.rttm"]
