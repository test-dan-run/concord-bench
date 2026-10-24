"""WER / CER and normalisers against jiwer called directly, on the fixture transcripts (M1)."""

from __future__ import annotations

import random

import jiwer
import pytest

from concord_bench.metrics import normalisers, wer
from conftest import dialogue_files


def perturb(text: str, seed: int) -> str:
    """Deterministic substitutions, deletions and insertions."""
    rng = random.Random(seed)
    out = []
    for w in text.split():
        x = rng.random()
        if x < 0.07:
            continue
        if x < 0.14:
            out.append(w[::-1])
        else:
            out.append(w)
        if rng.random() < 0.05:
            out.append("uh")
    return " ".join(out)


def utterances():
    for stem, f in dialogue_files().items():
        for k, t in enumerate(f["turns"]):
            yield f"{stem}#{k}", t["text"]


@pytest.mark.parametrize("norm", ["basic", "whisper_en", "none"])
def test_wer_matches_jiwer_per_utterance_and_corpus(norm):
    n = normalisers.get(norm)
    refs, hyps, rows = [], [], []
    for k, (_, text) in enumerate(utterances()):
        r, h = n(text), n(perturb(text, k))
        res = wer.score(r, h)
        direct = jiwer.process_words(r, h)
        assert (res.s, res.d, res.i, res.h) == (direct.substitutions, direct.deletions, direct.insertions, direct.hits)
        assert res.wer == pytest.approx(direct.wer)
        assert res.cer == pytest.approx(jiwer.cer(r, h))
        refs.append(r)
        hyps.append(h)
        rows.append(res.counts())
    agg = wer.aggregate(rows)
    assert agg["wer"] == pytest.approx(jiwer.wer(refs, hyps))
    assert agg["cer"] == pytest.approx(jiwer.cer(refs, hyps))


def test_alignment_ops_reconstruct_both_sides():
    r, h = "the cat sat on the mat", "the cat sat on mat today"
    res = wer.score(r, h)
    assert " ".join(o["ref"] for o in res.ops_json() if o["ref"]) == r
    assert " ".join(o["hyp"] for o in res.ops_json() if o["hyp"]) == h
    assert sum(o["op"] != "eq" for o in res.ops_json()) == res.errors


def test_empty_reference_and_hypothesis():
    assert wer.score("", "hello there").counts()["i"] == 2
    assert wer.score("", "").wer is None
    res = wer.score("a b c", "")
    assert (res.d, res.wer, res.cer) == (3, 1.0, 1.0)


def test_basic_normaliser():
    n = normalisers.get("basic")
    assert n("Hello, World! State-of-the-art: don't.") == "hello world state of the art dont"
    assert n("  Multiple   spaces\tand\nlines ") == "multiple spaces and lines"
    assert normalisers.get("basic@1").key == "basic@1"


def test_whisper_normaliser_numbers_and_spelling():
    n = normalisers.get("whisper_en")
    assert n("Mr. Smith has twenty five dogs.") == n("Mister Smith has 25 dogs")
    assert n("colour") == n("color")


def test_unknown_normaliser():
    with pytest.raises(KeyError):
        normalisers.get("nope")
    assert normalisers.get("platform:text-normalizer").service == "text-normalizer"


def test_platform_normaliser_batches(monkeypatch):
    import aicore

    sizes = []

    class Client:
        def transform(self, texts):
            sizes.append(len(texts))
            return [t.upper() for t in texts]

    monkeypatch.setattr(aicore, "client", lambda service: Client())
    texts = [f"Word {i}." for i in range(3997)]
    assert normalisers.get("platform:text-normalizer").many(texts) == [f"word {i}" for i in range(3997)]
    assert max(sizes) <= normalisers.PLATFORM_BATCH and sum(sizes) == 3997
