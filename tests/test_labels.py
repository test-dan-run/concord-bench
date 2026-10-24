"""EVALUATE_PLAN.md E5a: labels for slicing come with the references (NeMo `attribute` fields and the like)."""

from __future__ import annotations

import json
import wave

import numpy as np
from sqlalchemy import select

from concord_bench.core.models import AudioFile, Dataset, Segment
from concord_bench.datasets import labels
from concord_bench.datasets.ingest import ingest, inspect_source
from concord_bench.datasets.parsers import flatten, parse_file


def _tone(path, seconds=1.0, sr=16000):
    t = np.arange(int(seconds * sr)) / sr
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((0.1 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes())


def _nsc_like(root, n=30):
    """A NeMo manifest like NSC-Scripted's: one utterance per file, speaker attributes nested in `attribute`."""
    (root / "audio").mkdir(parents=True)
    lines = []
    for k in range(n):
        _tone(root / "audio" / f"u{k:03d}.wav")
        lines.append({"audio_filepath": f"audio/u{k:03d}.wav", "duration": 1.0, "text": f"utterance {k}",
                      "utt_id": f"SPK{k:04d}", "snr_db": 10.0 + k * 0.7,
                      "attribute": {"gender": "F" if k % 3 else "M", "ethnicity": ["Chinese", "Malay", "Indian"][k % 3],
                                    "device": {"mic": "close" if k % 2 else "far"}, "tags": ["x", "y"]},
                      "notes": "a long free-text note about how this utterance was recorded in the studio" * (k % 2)})
    (root / "manifest.json").write_text("\n".join(json.dumps(x) for x in lines))


def test_nested_objects_are_flattened_and_lists_dropped():
    assert flatten({"a": 1, "attribute": {"gender": "F", "device": {"mic": "far"}, "tags": [1]}, "e": ""}) == {
        "a": 1, "attribute.gender": "F", "attribute.device.mic": "far"}


def test_nemo_manifest_attributes_become_labels(tmp_path):
    _nsc_like(tmp_path)
    res = parse_file(tmp_path / "manifest.json")
    assert res.format == "nemo-manifest"
    meta = res.records[1].meta
    assert meta["attribute.gender"] == "F" and meta["attribute.ethnicity"] == "Malay"
    assert meta["attribute.device.mic"] == "close" and "attribute.tags" not in meta
    assert "duration" not in meta and "text" not in meta and "audio_filepath" not in meta


def test_label_kinds():
    assert labels.kind_of(["F", "M", "F"]) == ("discrete", None)
    assert labels.kind_of([True, False]) == ("discrete", None)
    assert labels.kind_of([1, 2, 2, 3]) == ("discrete", None)
    assert labels.kind_of([float(k) / 3 for k in range(40)]) == ("continuous", None)
    assert labels.kind_of([str(k / 3) for k in range(40)]) == ("continuous", None)  # numbers written as text
    kind, why = labels.kind_of([f"SPK{k}" for k in range(40)])
    assert kind == "unused" and "ID" in why
    kind, why = labels.kind_of(list(range(40)))
    assert kind == "unused" and "ID" in why
    kind, why = labels.kind_of(["x" * 60, "y" * 60])
    assert kind == "unused" and why == "free text"


def test_ingest_stores_labels_on_segments_and_files_and_reports_them(session, tmp_path):
    _nsc_like(tmp_path / "src")
    preview = inspect_source(tmp_path / "src")
    by_key = {lb["key"]: lb for lb in preview["labels"]}
    assert by_key["attribute.gender"]["kind"] == "discrete" and by_key["attribute.gender"]["level"] == "file"
    assert {v["value"] for v in by_key["attribute.ethnicity"]["values"]} == {"Chinese", "Malay", "Indian"}
    assert by_key["snr_db"]["kind"] == "continuous" and by_key["snr_db"]["range"]["min"] == 10.0
    assert by_key["utt_id"]["kind"] == "unused" and "ID" in by_key["utt_id"]["reason"]
    assert by_key["notes"]["kind"] == "unused" and by_key["notes"]["have"] == 15

    ds = Dataset(name="nsc-like")
    session.add(ds)
    session.flush()
    report = ingest(session, ds.id, tmp_path / "src")
    session.commit()
    assert report["labels"] == preview["labels"]
    f = session.scalar(select(AudioFile).where(AudioFile.rel_path == "audio/u001.wav"))
    seg = session.scalar(select(Segment).where(Segment.audio_file_id == f.id))
    assert seg.meta["attribute.gender"] == "F" and f.meta["attribute.gender"] == "F"


def test_only_labels_constant_across_a_file_go_on_the_file(session, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tone(src / "meeting.wav", 4.0)
    (src / "segments.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"file": "meeting", "start": 0.0, "end": 2.0, "speaker": "A", "text": "hi there", "room": "plaza",
         "attribute": {"gender": "F"}},
        {"file": "meeting", "start": 2.0, "end": 4.0, "speaker": "B", "text": "hello", "room": "plaza",
         "attribute": {"gender": "M"}}]))
    ds = Dataset(name="meeting")
    session.add(ds)
    session.flush()
    report = ingest(session, ds.id, src)
    session.commit()
    f = session.scalar(select(AudioFile))
    assert f.meta == {"room": "plaza"}
    segs = session.scalars(select(Segment).order_by(Segment.start)).all()
    assert [s.meta["attribute.gender"] for s in segs] == ["F", "M"]
    gender = next(lb for lb in report["labels"] if lb["key"] == "attribute.gender")
    assert gender["level"] == "utterance" and gender["have"] == 2


def test_bench_keys_in_segment_meta_are_not_labels():
    assert labels.of({"source_uid": True, "room": "x"}) == {"room": "x"}
    assert [lb["key"] for lb in labels.summarize([({"source_uid": True, "room": "x"}, True)])] == ["room"]
