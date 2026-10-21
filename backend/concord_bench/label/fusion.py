"""Ensemble fusion for pseudo-labelling (PLAN §6.2). Pure functions, no I/O.

For one segment, every ASR member gives a hypothesis. Comparison always happens after the `basic`
normaliser, so case and punctuation never count as disagreement; the text written to the segment is
the raw text (what an annotator wants to start from).

- best-aligned: the member hypothesis with the lowest summed WER to the other members (ties go to a
  word-timed member).
- ROVER: word-timed members only. Their words are aligned to a backbone (the best-aligned word-timed
  member) and each slot is decided by vote, ties going to the backbone. With fewer than two word-timed
  members the segment falls back to best-aligned.
- single: one member's hypothesis.

Confidence = member agreement: the mean, over the other members, of 1 - WER(member as reference,
starting hypothesis), clipped to [0, 1]. With nobody to agree with it is None (always reviewed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.models import SegmentStatus
from ..metrics import wer
from ..metrics.normalisers import basic_one

STRATEGIES = ("best_aligned", "rover", "single")
DEFAULT_HIGH = 0.85
DEFAULT_LOW = 0.50


@dataclass
class Hyp:
    member: str
    text: str
    words: list[dict[str, Any]] | None = None  # [{"w", "start", "end"}] in file time
    norm: str = field(init=False)

    def __post_init__(self) -> None:
        self.norm = basic_one(self.text or "")


@dataclass
class Fused:
    text: str
    words: list[dict[str, Any]] | None
    chosen: str  # member name, or "rover"
    strategy: str  # what was actually applied (rover may fall back to best_aligned)
    confidence: float | None
    agreement: dict[str, float]  # member -> agreement with the starting hypothesis


def errors(ref: str, hyp: str) -> tuple[int, int]:
    """(word errors, reference words) of hyp against ref, both already normalised."""
    r = wer.align(ref.split(), hyp.split())
    return r.errors, r.n


def agreement(ref: str, hyp: str) -> float:
    e, n = errors(ref, hyp)
    if n == 0:
        return 1.0 if e == 0 else 0.0
    return max(0.0, 1.0 - e / n)


def best_aligned(hyps: list[Hyp]) -> int:
    """Index of the hypothesis with the lowest summed WER to the others. Ties (common with two members of
    the same length, whose costs are then symmetric) go to a word-timed member, whose times make splits
    exact, then to the first one."""
    if len(hyps) == 1:
        return 0
    costs = []
    for i, h in enumerate(hyps):
        c = 0.0
        for j, o in enumerate(hyps):
            if i != j:
                e, n = errors(o.norm, h.norm)
                c += e / n if n else float(e > 0)
        costs.append(c)
    return min(range(len(hyps)), key=lambda i: (round(costs[i], 9), not hyps[i].words, i))


# ---------------------------------------------------------------- ROVER

def _key(word: str) -> str:
    return basic_one(word).replace(" ", "_")


def _keyed(words: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Words with their comparison key; punctuation-only words have no key and are dropped."""
    return [(k, w) for w in words if (k := _key(w["w"]))]


@dataclass
class _Slot:
    votes: dict[str, tuple[str, dict[str, Any] | None]] = field(default_factory=dict)  # member -> (key, word)


def rover(hyps: list[Hyp], backbone: int) -> tuple[str, list[dict[str, Any]]]:
    """Fused (text, words) from word-timed hypotheses; `backbone` indexes into `hyps`."""
    base = hyps[backbone]
    base_words = _keyed(base.words or [])
    slots = [_Slot({base.member: (k, w)}) for k, w in base_words]
    # Words a member has where the backbone has none, by backbone position (inserted before slot k).
    inserted: dict[int, list[_Slot]] = {}
    for h in hyps:
        if h is base:
            continue
        mine = _keyed(h.words or [])
        res = wer.align([k for k, _ in base_words], [k for k, _ in mine])
        pos = 0
        ins_at: dict[int, int] = {}
        for op in res.ops:
            if op.op in ("eq", "sub"):
                slots[op.ri].votes[h.member] = mine[op.hi]
                pos = op.ri + 1
            elif op.op == "del":
                slots[op.ri].votes[h.member] = ("", None)
                pos = op.ri + 1
            else:  # insertion before backbone slot `pos`
                k, w = mine[op.hi]
                group = inserted.setdefault(pos, [])
                start = ins_at.get(pos, 0)
                # Join an earlier member's inserted slot with the same word, else open a new one.
                hit = next((n for n in range(start, len(group)) if any(v[0] == k for v in group[n].votes.values())),
                           None)
                if hit is None:
                    group.append(_Slot({h.member: (k, w)}))
                    hit = len(group) - 1
                else:
                    group[hit].votes[h.member] = (k, w)
                ins_at[pos] = hit + 1
    members = [h.member for h in hyps]
    order: list[tuple[_Slot, bool]] = []
    for k in range(len(slots) + 1):
        order += [(s, False) for s in inserted.get(k, [])]
        if k < len(slots):
            order.append((slots[k], True))
    out: list[dict[str, Any]] = []
    for slot, on_backbone in order:
        tally: dict[str, int] = {}
        for m in members:
            key = slot.votes.get(m, ("", None))[0]
            tally[key] = tally.get(key, 0) + 1
        best = max(tally.values())
        winners = [k for k, v in tally.items() if v == best]
        base_key = slot.votes.get(base.member, ("", None))[0] if on_backbone else ""
        win = base_key if base_key in winners else ("" if "" in winners else winners[0])
        if not win:
            continue
        voter = base.member if slot.votes.get(base.member, ("",))[0] == win else \
            next(m for m in members if slot.votes.get(m, ("",))[0] == win)
        out.append(dict(slot.votes[voter][1]))
    return " ".join(w["w"] for w in out), out


# ---------------------------------------------------------------- one segment

def fuse(hyps: list[Hyp], strategy: str = "best_aligned", single: str | None = None) -> Fused:
    if not hyps:
        raise ValueError("no hypotheses to fuse")
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; use one of {', '.join(STRATEGIES)}")
    applied = strategy
    words: list[dict[str, Any]] | None
    if strategy == "single":
        idx = next((i for i, h in enumerate(hyps) if h.member == single), None)
        if idx is None:
            idx, applied = best_aligned(hyps), "best_aligned"
        text, words, chosen = hyps[idx].text, hyps[idx].words, hyps[idx].member
    elif strategy == "rover" and sum(1 for h in hyps if h.words) >= 2:
        timed = [h for h in hyps if h.words]
        text, words = rover(timed, best_aligned(timed))
        chosen = "rover"
    else:
        if strategy == "rover":
            applied = "best_aligned"
        idx = best_aligned(hyps)
        text, words, chosen = hyps[idx].text, hyps[idx].words, hyps[idx].member
    start = basic_one(text)
    others = [h for h in hyps if h.member != chosen]
    agree = {h.member: agreement(h.norm, start) for h in others}
    conf = sum(agree.values()) / len(agree) if agree else None
    return Fused(text.strip(), words, chosen, applied, conf, agree)


def triage(confidence: float | None, high: float = DEFAULT_HIGH, low: float = DEFAULT_LOW) -> str:
    """Segment status from confidence: auto-accept, review (pseudo) or reject."""
    if confidence is None:
        return SegmentStatus.pseudo
    if confidence >= high:
        return SegmentStatus.auto
    if confidence >= low:
        return SegmentStatus.pseudo
    return SegmentStatus.rejected


# ---------------------------------------------------------------- dataset-level summaries

class AgreementMatrix:
    """Pairwise corpus WER between members: cell (i, j) = errors of j against i as reference / words of i."""

    def __init__(self, members: list[str]):
        self.members = members
        self.err: dict[tuple[str, str], int] = {}
        self.n: dict[tuple[str, str], int] = {}

    def add(self, hyps: list[Hyp]) -> None:
        for a in hyps:
            for b in hyps:
                if a.member == b.member:
                    continue
                e, n = errors(a.norm, b.norm)
                self.err[(a.member, b.member)] = self.err.get((a.member, b.member), 0) + e
                self.n[(a.member, b.member)] = self.n.get((a.member, b.member), 0) + n

    def to_json(self) -> dict[str, Any]:
        cells = []
        for a in self.members:
            for b in self.members:
                if a == b:
                    continue
                n = self.n.get((a, b), 0)
                cells.append({"ref": a, "hyp": b, "wer": self.err.get((a, b), 0) / n if n else None, "n": n})
        return {"members": self.members, "cells": cells}
