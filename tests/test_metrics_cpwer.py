"""cpWER against meeteval called directly; per-word errors; attribution arithmetic (M1)."""

from __future__ import annotations

import pytest
from meeteval.wer.wer.cp import cp_word_error_rate

from concord_bench.metrics import attribution, cpwer, normalisers
from concord_bench.metrics.cpwer import SpkSegment
from conftest import dialogue_files

N = normalisers.get("basic")


def ref_segments(f) -> list[SpkSegment]:
    return [SpkSegment(t["start"], t["end"], t["speaker"], N(t["text"])) for t in f["turns"]]


def hyp_variant(f, kind: str) -> list[SpkSegment]:
    names = sorted({t["speaker"] for t in f["turns"]})
    out = []
    for k, t in enumerate(f["turns"]):
        words = N(t["text"]).split()
        spk = f"h{names.index(t['speaker'])}"
        if kind == "wrong_speaker" and k % 3 == 1:
            spk = f"h{(names.index(t['speaker']) + 1) % len(names)}"
        if kind == "asr" and len(words) > 3:
            words[1] = "zzz"
            del words[2]
        if kind == "merged":
            spk = "h0"
        out.append(SpkSegment(t["start"], t["end"], spk, " ".join(words)))
    return out


@pytest.mark.parametrize("stem", list(dialogue_files()))
@pytest.mark.parametrize("kind", ["perfect", "asr", "wrong_speaker", "merged"])
def test_cpwer_matches_meeteval(stem, kind):
    f = dialogue_files()[stem]
    ref, hyp = ref_segments(f), hyp_variant(f, kind)
    ours = cpwer.cpwer(ref, hyp)
    direct = cp_word_error_rate(cpwer.streams(ref), cpwer.streams(hyp), reference_sort=False, hypothesis_sort=False)
    assert ours["errors"] == direct.errors
    assert ours["n"] == direct.length
    assert ours["cpwer"] == pytest.approx(direct.error_rate)
    # per-word re-alignment gives the same total
    assert cpwer.per_word(ours)["errors"] == direct.errors
    if kind == "perfect":
        assert ours["errors"] == 0


def test_wrong_speaker_words_counted_twice_and_listed():
    ref = [SpkSegment(0, 1, "A", "hello there"), SpkSegment(1, 2, "B", "yes please")]
    hyp = [SpkSegment(0, 1, "x", "hello"), SpkSegment(1, 2, "y", "there yes please")]
    res = cpwer.cpwer(ref, hyp)
    assert res["errors"] == 2  # "there": deleted from A, inserted into B
    pw = cpwer.per_word(res)
    assert pw["wrong_speaker_words"] == [{"word": "there", "ref_speaker": "A", "hyp_speaker": "y", "given_to": "B"}]


def test_streams_are_time_ordered():
    segs = [SpkSegment(5, 6, "A", "second"), SpkSegment(0, 1, "A", "first")]
    assert cpwer.streams(segs) == {"A": "first second"}


def test_attribution_split_and_aggregate():
    s = attribution.split(a_errors=10, b_errors=14, c_errors=20, n=100)
    assert (s["asr"], s["segmentation"], s["speaker"], s["cpwer"]) == (0.10, pytest.approx(0.04), 0.06, 0.20)
    assert s["asr"] + s["segmentation"] + s["speaker"] == pytest.approx(s["cpwer"])
    neg = attribution.split(10, 8, 12, 100)
    assert neg["segmentation"] == pytest.approx(-0.02)  # raw in tables
    assert attribution.clipped(neg)["segmentation"] == 0.0  # clipped in charts
    agg = attribution.aggregate([s, neg])
    assert agg["cpwer"] == pytest.approx(32 / 200)


def test_best_ref_speaker():
    turns = [(0, 4, "A"), (3, 6, "B"), (6.5, 7, "A")]
    assert attribution.best_ref_speaker(3.2, 6.8, turns) == "B"
    assert attribution.best_ref_speaker(10, 11, turns) is None
