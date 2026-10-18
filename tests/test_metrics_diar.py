"""DER / JER / VAD against pyannote.metrics called directly on the fixture turns (M1)."""

from __future__ import annotations

import pytest
from pyannote.core import Annotation, Segment, Timeline
from pyannote.metrics.detection import DetectionErrorRate
from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate

from concord.metrics import der, vad
from concord.metrics.der import Turn
from conftest import dialogue_files


def perturbed(turns: list[dict], shift: float, swap_every: int) -> list[Turn]:
    """Shift boundaries, rename speakers, give every n-th turn to the wrong speaker, drop one turn."""
    names = sorted({t["speaker"] for t in turns})
    out = []
    for k, t in enumerate(turns):
        if k == 2:
            continue
        spk = names[(names.index(t["speaker"]) + (1 if k % swap_every == 0 else 0)) % len(names)]
        out.append(Turn(max(0.0, t["start"] + shift), t["end"] - shift / 2, f"hyp_{spk}"))
    return out


def ann(turns):
    a = Annotation()
    for k, t in enumerate(turns):
        a[Segment(t.start, t.end), k] = t.speaker
    return a


CASES = [(stem, shift, swap) for stem in dialogue_files() for shift, swap in ((0.0, 99), (0.15, 4), (-0.3, 3))]


@pytest.mark.parametrize("stem,shift,swap", CASES)
@pytest.mark.parametrize("collar", [0.0, 0.25])
def test_der_jer_vad_match_pyannote(stem, shift, swap, collar):
    f = dialogue_files()[stem]
    ref = [Turn(t["start"], t["end"], t["speaker"]) for t in f["turns"]]
    hyp = perturbed(f["turns"], shift, swap)
    dur = f["duration"]
    uem = Timeline([Segment(0, dur)])
    ours = der.der(ref, hyp, dur, collar)
    direct = DiarizationErrorRate(collar=2 * collar)(ann(ref), ann(hyp), uem=uem, detailed=True)
    assert ours["missed"] == pytest.approx(direct["missed detection"])
    assert ours["false_alarm"] == pytest.approx(direct["false alarm"])
    assert ours["confusion"] == pytest.approx(direct["confusion"])
    assert ours["der"] == pytest.approx(direct["diarization error rate"])
    assert ours["jer"] == pytest.approx(JaccardErrorRate(collar=2 * collar)(ann(ref), ann(hyp), uem=uem))
    v = vad.detection(ref, hyp, dur, collar)
    dv = DetectionErrorRate(collar=2 * collar)(ann(ref), ann(hyp), uem=uem, detailed=True)
    assert v["vad_missed"] == pytest.approx(dv["miss"])
    assert v["vad_false_alarm"] == pytest.approx(dv["false alarm"])
    assert v["vad_error"] == pytest.approx(dv["detection error rate"])


def test_corpus_der_sums_seconds():
    rows = []
    metric = DiarizationErrorRate(collar=0.5)
    for f in dialogue_files().values():
        ref = [Turn(t["start"], t["end"], t["speaker"]) for t in f["turns"]]
        hyp = perturbed(f["turns"], 0.2, 3)
        rows.append(der.der(ref, hyp, f["duration"], 0.25))
        metric(ann(ref), ann(hyp), uem=Timeline([Segment(0, f["duration"])]))
    assert der.aggregate(rows)["der"] == pytest.approx(abs(metric))


def test_perfect_hypothesis_and_mapping():
    f = next(iter(dialogue_files().values()))
    ref = [Turn(t["start"], t["end"], t["speaker"]) for t in f["turns"]]
    hyp = [Turn(t.start, t.end, f"x_{t.speaker}") for t in ref]
    d = der.der(ref, hyp, f["duration"])
    assert d["der"] == pytest.approx(0.0)
    assert d["mapping"] == {f"x_{s}": s for s in {t.speaker for t in ref}}
    assert der.error_regions(ref, hyp, f["duration"], d["mapping"]) == []


def test_error_regions_types():
    ref = [Turn(0, 4, "A"), Turn(4, 8, "B")]
    hyp = [Turn(1, 4, "a"), Turn(4, 6, "a"), Turn(6, 9, "b")]
    regions = der.error_regions(ref, hyp, 10, {"a": "A", "b": "B"})
    kinds = {(r["type"], r["start"], r["end"]) for r in regions}
    assert ("missed", 0.0, 1.0) in kinds
    assert ("confusion", 4.0, 6.0) in kinds
    assert ("false_alarm", 8.0, 9.0) in kinds


def test_overlaps_from_fixture():
    f = dialogue_files()["d03_44k_stereo"]
    ref = [Turn(t["start"], t["end"], t["speaker"]) for t in f["turns"]]
    ov = der.overlaps(ref, f["duration"])
    assert ov, "d03 has overlapped turns by construction"
    assert all(o["end"] > o["start"] for o in ov)
