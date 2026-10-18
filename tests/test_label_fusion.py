"""Ensemble fusion (label/fusion.py): best-aligned, ROVER, confidence and triage."""

from __future__ import annotations

import pytest

from concord.label.fusion import AgreementMatrix, Hyp, agreement, best_aligned, fuse, rover, triage


def timed(member: str, text: str, t0: float = 0.0) -> Hyp:
    words = [{"w": w, "start": t0 + 0.5 * k, "end": t0 + 0.5 * k + 0.4} for k, w in enumerate(text.split())]
    return Hyp(member, text, words)


def test_best_aligned_picks_the_consensus():
    hyps = [Hyp("a", "the cat sat on the mat"), Hyp("b", "the cat sat on a mat"), Hyp("c", "a dog stood there")]
    assert best_aligned(hyps) in (0, 1)
    f = fuse(hyps, "best_aligned")
    assert f.chosen in ("a", "b") and f.strategy == "best_aligned"
    assert set(f.agreement) == {"a", "b", "c"} - {f.chosen}


def test_case_and_punctuation_are_not_disagreement():
    f = fuse([Hyp("a", "Hello, Riverside dental."), Hyp("b", "hello riverside dental")])
    assert f.confidence == pytest.approx(1.0)
    assert f.text in ("Hello, Riverside dental.", "hello riverside dental")


def test_confidence_is_mean_agreement():
    f = fuse([Hyp("a", "one two three four"), Hyp("b", "one two three four"), Hyp("c", "one two three five")])
    assert f.chosen in ("a", "b")
    # vs the twin: 1.0; vs c: 1 - 1/4
    assert f.confidence == pytest.approx((1.0 + 0.75) / 2)


def test_single_member_has_no_confidence_and_is_reviewed():
    f = fuse([Hyp("a", "anything")])
    assert f.confidence is None and triage(f.confidence) == "pseudo"


def test_single_strategy_uses_that_member_and_others_still_vote():
    f = fuse([Hyp("a", "good morning"), Hyp("b", "good evening")], "single", single="b")
    assert f.chosen == "b" and f.text == "good evening" and f.confidence == pytest.approx(0.5)


def test_rover_majority_fixes_the_backbone():
    hyps = [timed("a", "book a check up please"), timed("b", "book a cheque up please"),
            timed("c", "book the check up please")]
    text, words = rover(hyps, 1)  # backbone deliberately the odd one out on "cheque"
    assert text == "book a check up please"
    assert [w["start"] for w in words] == sorted(w["start"] for w in words)


def test_rover_inserted_word_needs_a_majority():
    hyps = [timed("a", "call me back"), timed("b", "call me right back"), timed("c", "call me right back")]
    assert rover(hyps, 0)[0] == "call me right back"
    hyps = [timed("a", "call me back"), timed("b", "call me right back"), timed("c", "call me back")]
    assert rover(hyps, 0)[0] == "call me back"


def test_rover_keeps_raw_words_and_drops_punctuation_tokens():
    hyps = [timed("a", "Yes , of course."), timed("b", "yes of course")]
    text, _ = rover(hyps, 0)
    assert text == "Yes of course."


def test_rover_falls_back_without_two_word_timed_members():
    f = fuse([timed("a", "hello there"), Hyp("b", "hello there")], "rover")
    assert f.strategy == "best_aligned" and f.chosen in ("a", "b")
    f = fuse([timed("a", "hello there"), timed("b", "hello where"), Hyp("c", "hello there")], "rover")
    assert f.strategy == "rover" and f.chosen == "rover" and f.words


def test_triage_thresholds():
    assert triage(0.9) == "auto" and triage(0.85) == "auto"
    assert triage(0.84) == "pseudo" and triage(0.5) == "pseudo"
    assert triage(0.49) == "rejected"
    assert triage(0.7, high=0.6) == "auto"


def test_agreement_edge_cases():
    assert agreement("", "") == 1.0
    assert agreement("", "extra") == 0.0
    assert agreement("a b", "x y z w") == 0.0  # clipped at 0


def test_agreement_matrix():
    m = AgreementMatrix(["a", "b"])
    m.add([Hyp("a", "one two"), Hyp("b", "one three")])
    cells = {(c["ref"], c["hyp"]): c for c in m.to_json()["cells"]}
    assert cells[("a", "b")]["wer"] == pytest.approx(0.5) and cells[("b", "a")]["n"] == 2


def test_unknown_strategy():
    with pytest.raises(ValueError):
        fuse([Hyp("a", "x")], "vote")
