"""EVALUATE_PLAN.md E5c and E6: slices by label with bootstrap intervals and patterns, saved slices, the
diarisation regions and speaker matrices, and each pipeline error word's cause."""

from __future__ import annotations

import pytest

from concord_bench.analysis import diar as diar_an
from concord_bench.analysis import slices as sl
from concord_bench.core.models import Result
from concord_bench.metrics import cpwer
from concord_bench.metrics.cpwer import SpkSegment
from concord_bench.runs import scoring


def _u(i, errors, n=10, **labels):
    return sl.Unit(i, i, f"u{i}", {"n": n, "s": errors, "d": 0, "i": 0, "h": n - errors, "cer_n": 0, "cer_e": 0},
                   labels, errors / n)


def _units():
    """60 utterances: accent B is reliably worse (4/10 errors), A is the rest (1/10)."""
    return [_u(k, 4 if k % 3 == 0 else 1, accent="B" if k % 3 == 0 else "A", snr=float(k)) for k in range(60)]


def test_whole_number_floats_are_continuous_not_ids():
    from concord_bench.datasets import labels

    assert labels.kind_of([float(k) for k in range(60)]) == ("continuous", None)
    assert labels.kind_of(list(range(60)))[0] == "unused"


def test_slice_scores_come_from_summed_counts_with_seeded_intervals():
    us = _units()
    assert sl.rate(us, "wer") == pytest.approx((20 * 4 + 40 * 1) / 600)
    assert sl.interval(us, "wer") == sl.interval(us, "wer")  # seeded: the same data gives the same interval
    lo, hi = sl.interval(us, "wer")
    assert lo < sl.rate(us, "wer") < hi


def test_patterns_worse_better_too_few_and_hard():
    us = _units()
    rows = {r["value"]: r for r in sl.label_rows(us, "accent", "wer", "asr")}
    assert rows["B"]["pattern"] == "worse" and rows["A"]["pattern"] == "better"
    assert rows["B"]["n"] == 20 and rows["B"]["delta"] > 0
    few = sl.label_rows(us[:12], "accent", "wer", "asr")
    assert {r["pattern"] for r in few} == {"too_few"}
    # the comparison model is also worse on B: the audio is hard, not this model
    other = [_u(k, 5 if k % 3 == 0 else 2, accent="B" if k % 3 == 0 else "A") for k in range(60)]
    rows = {r["value"]: r for r in sl.label_rows(us, "accent", "wer", "asr", other)}
    assert rows["B"]["pattern"] == "hard" and rows["B"]["compare"]["n"] == 20
    finder = sl.finder(us, "wer", "asr", other)
    assert finder[0]["value"] == "B" and finder[0]["pattern"] == "hard"


def test_continuous_labels_bin_by_width_quantile_or_custom_edges():
    us = _units()
    even = sl.groups(us, "snr")
    assert len(even) == sl.DEFAULT_BINS and sum(len(g) for _, g in even) == 60
    quant = sl.groups(us, "snr", "quantile")
    assert {len(g) for _, g in quant} == {12}  # equal counts
    custom = sl.groups(us, "snr", "custom", [10.0, 50.0])
    assert [len(g) for _, g in custom] == [10, 40, 10]
    assert custom[0][0]["kind"] == "continuous" and custom[0][0]["min"] == 0.0


def test_heatmap_cells_and_saved_slice_filters():
    us = _units()
    hm = sl.heatmap(us, "accent", "snr", "wer")
    assert [r["label"] for r in hm["rows"]] == ["A", "B"] and len(hm["cols"]) == sl.DEFAULT_BINS
    assert all(c["colour"] == (c["n"] >= sl.HEAT_MIN) for r in hm["rows"] for c in r["cells"])
    assert sum(sl.matches(u, [{"key": "accent", "value": "B"}]) for u in us) == 20
    assert sum(sl.matches(u, [{"key": "accent", "value": "B"}, {"key": "snr", "min": 30}]) for u in us) == 10
    assert not sl.matches(us[0], [{"key": "missing", "value": 1}])


def test_saved_slices_belong_to_one_dataset(client, session):
    from concord_bench.core.models import Dataset

    a, b = Dataset(name="a", status="ready"), Dataset(name="b", status="ready")
    session.add_all([a, b])
    session.commit()
    r = client.post(f"/api/datasets/{a.id}/slices", json={"name": "accent B", "filters": [{"key": "accent", "value": "B"}]})
    assert r.status_code == 201
    assert [s["name"] for s in client.get(f"/api/datasets/{a.id}/slices").json()] == ["accent B"]
    assert client.get(f"/api/datasets/{b.id}/slices").json() == []
    assert client.post(f"/api/datasets/{a.id}/slices", json={"name": "x", "filters": [{"key": "k"}]}).status_code == 422
    assert client.delete(f"/api/slices/{r.json()['id']}").status_code == 204
    session.delete(a)
    session.commit()


# ---------------------------------------------------------------- pipeline causes

def test_moment_kinds():
    segs = [SpkSegment(0.0, 2.0, "A", "x"), SpkSegment(2.2, 2.8, "B", "y"), SpkSegment(2.5, 40.0, "A", "z")]
    kinds = scoring.moment_kinds(segs)
    assert kinds[0] == {"near_change"}
    assert kinds[1] == {"short", "near_change", "overlap"}
    assert {"long", "overlap"} <= kinds[2]


def test_every_error_word_gets_one_cause_and_they_add_up():
    ref = [SpkSegment(0.0, 2.0, "A", "the cat sat"), SpkSegment(2.0, 4.0, "B", "on the mat")]
    hyp_a = [SpkSegment(0.0, 2.0, "A", "the cat sad"), SpkSegment(2.0, 4.0, "B", "on the mat")]  # ASR: sat->sad
    hyp_b = [SpkSegment(0.0, 3.0, "A", "the cat sad on"), SpkSegment(3.0, 4.0, "B", "the mat")]  # cut late
    hyp_c = [SpkSegment(0.0, 3.0, "x", "the cat sad on"), SpkSegment(3.0, 4.0, "x", "the mat")]  # one speaker
    a, b, c = cpwer.cpwer(ref, hyp_a), cpwer.cpwer(ref, hyp_b), cpwer.cpwer(ref, hyp_c)
    causes, moments, lines = scoring.word_causes(ref, a, b, c)
    tags = [t for spk in causes.values() for t in spk if t]
    wrong_c = sum(len(v) for v in scoring._wrong_words(c).values())
    assert len(tags) == wrong_c > 0
    assert causes["A"][2] == "asr"  # "sat" is wrong even with reference segments
    assert "speaker" in tags  # B's words went to A's speaker
    assert [w["w"] for w in lines[0]["words"]] == ["the", "cat", "sat"]
    assert moments["near_change"]["words"] == 6


# ---------------------------------------------------------------- diarisation regions and matrices

def _diar_result(ref, hyp, errors, dur=10.0, ref_spk=2, hyp_spk=1):
    return Result(id=1, score_id=1, audio_file_id=1, utterance_key="f", start=0.0, end=dur,
                  metrics={"ref_speakers": ref_spk, "hyp_speakers": hyp_spk},
                  alignment={"ref": ref, "hyp": hyp, "errors": errors, "mapping": {}})


def test_regions_share_lift_and_mix():
    ref = [[0.0, 5.0, "A"], [4.0, 8.0, "B"], [8.2, 8.6, "A"]]  # 1 s overlap, a short turn, a change at 8.0-8.2
    errors = [{"type": "missed", "start": 4.0, "end": 5.0, "dur": 1.0},
              {"type": "false_alarm", "start": 9.0, "end": 9.5, "dur": 0.5}]
    out = diar_an.regions([_diar_result(ref, [], errors)], {1: "f"})
    rows = {r["key"]: r for r in out["rows"]}
    assert rows["overlap"]["share_errors"] == pytest.approx(1.0 / 1.5, abs=0.02)
    assert rows["overlap"]["mix"]["missed"] == pytest.approx(1.0)
    assert rows["nonspeech"]["mix"]["false_alarm"] == pytest.approx(1.0)
    assert rows["overlap"]["lift"] > 1 and rows["overlap"]["worst"][0]["type"] == "missed"
    assert rows["short"]["seconds"] == pytest.approx(0.4, abs=0.02)


def test_speaker_count_and_confusion_matrices():
    counts = diar_an.speaker_counts([_diar_result([], [], [], ref_spk=3, hyp_spk=2),
                                     _diar_result([], [], [], ref_spk=2, hyp_spk=2)])
    assert counts["exact"] == 1 and counts["under"] == 1 and counts["cols"] == [2]
    r = _diar_result([[0.0, 4.0, "A"], [4.0, 8.0, "B"]], [[0.0, 8.0, "x"]], [])
    cm = diar_an.confusion(r)
    assert cm["cols"] == ["x"] and [row["cells"][0] for row in cm["rows"]] == pytest.approx([4.0, 4.0], abs=0.02)
    assert cm["notes"] == [{"kind": "merge", "hyp": "x", "ref": ["A", "B"]}]
