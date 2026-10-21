"""cpWER (meeteval): concatenated minimum-permutation WER over speaker-attributed transcripts.

Each reference speaker's words (in time order) are matched to one hypothesis speaker's words; the
assignment minimising total errors wins. A word given to the wrong speaker is a deletion in the
right speaker's stream and an insertion in the wrong one, so it counts twice.

`per_word` re-aligns each assigned pair with `wer.align` to list individual errors; the totals equal
meeteval's (both are Levenshtein distances), which the tests check.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from . import wer as wer_mod


@dataclass(frozen=True)
class SpkSegment:
    start: float
    end: float
    speaker: str
    text: str  # normalised


def streams(segments: list[SpkSegment]) -> dict[str, str]:
    """Speaker -> that speaker's words concatenated in start-time order."""
    by: dict[str, list[SpkSegment]] = {}
    for s in sorted(segments, key=lambda s: (s.start, s.end)):
        by.setdefault(s.speaker, []).append(s)
    return {spk: " ".join(t for t in (s.text.strip() for s in segs) if t) for spk, segs in by.items()}


def cpwer(ref: list[SpkSegment], hyp: list[SpkSegment]) -> dict[str, Any]:
    from meeteval.wer.wer.cp import cp_word_error_rate

    r, h = streams(ref), streams(hyp)
    if not r:
        r = {"__none__": ""}
    if not h:
        h = {"__none__": ""}
    # Streams are already in time order, so meeteval must not re-sort them.
    res = cp_word_error_rate(r, h, reference_sort=False, hypothesis_sort=False)
    assignment = [[a, b] for a, b in res.assignment]
    return {
        "cpwer": res.error_rate if res.length else None, "errors": res.errors, "n": res.length,
        "s": res.substitutions, "d": res.deletions, "i": res.insertions,
        "missed_speakers": res.missed_speaker, "falarm_speakers": res.falarm_speaker,
        "scored_speakers": res.scored_speaker, "assignment": assignment,
        "_streams": (r, h),
    }


def per_word(result: dict[str, Any]) -> dict[str, Any]:
    """Per-pair alignments and wrong-speaker word pairs for a `cpwer()` result."""
    r, h = result["_streams"]
    pairs = []
    deletions: list[tuple[str, str]] = []  # (ref speaker, word)
    insertions: list[tuple[str, str]] = []  # (hyp speaker, word)
    for ref_spk, hyp_spk in result["assignment"]:
        rt = r.get(ref_spk, "").split() if ref_spk is not None else []
        ht = h.get(hyp_spk, "").split() if hyp_spk is not None else []
        al = wer_mod.align(rt, ht)
        pairs.append({"ref_speaker": ref_spk, "hyp_speaker": hyp_spk, **al.counts(), "ops": al.ops_json()})
        deletions += [(ref_spk, o.ref) for o in al.ops if o.op == "del"]
        insertions += [(hyp_spk, o.hyp) for o in al.ops if o.op == "ins"]
    # A deleted word that some *other* hypothesis speaker inserted is a wrong-speaker word.
    ins_pool = Counter(w for _, w in insertions)
    owner = {hs: rs for rs, hs in result["assignment"]}
    ins_by_word: dict[str, list[str]] = {}
    for hs, w in insertions:
        ins_by_word.setdefault(w, []).append(hs)
    wrong = []
    for rs, w in deletions:
        if ins_pool[w] <= 0:
            continue
        for k, hs in enumerate(ins_by_word.get(w, [])):
            if owner.get(hs) != rs:
                ins_pool[w] -= 1
                ins_by_word[w].pop(k)
                wrong.append({"word": w, "ref_speaker": rs, "hyp_speaker": hs, "given_to": owner.get(hs)})
                break
    return {"pairs": pairs, "wrong_speaker_words": wrong,
            "errors": sum(p["errors"] for p in pairs)}


def public(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tot = {k: sum(r.get(k) or 0 for r in rows) for k in ("errors", "n", "s", "d", "i")}
    tot["cpwer"] = tot["errors"] / tot["n"] if tot["n"] else None
    tot["files"] = len(rows)
    return tot
