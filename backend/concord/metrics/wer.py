"""WER / CER with jiwer, plus the word alignment used for inline diffs.

Inputs are already-normalised strings. An empty reference gives N = 0 and `wer = None` (every
hypothesis word is an insertion); corpus WER sums errors over N across utterances.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import jiwer


@dataclass
class AlignOp:
    op: str  # eq | sub | del | ins
    ref: str | None
    hyp: str | None
    ri: int | None  # index into the reference tokens
    hi: int | None  # index into the hypothesis tokens


@dataclass
class WerResult:
    n: int
    s: int
    d: int
    i: int
    h: int
    cer_n: int  # reference characters
    cer_e: int  # character edits
    ops: list[AlignOp] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return self.s + self.d + self.i

    @property
    def wer(self) -> float | None:
        return self.errors / self.n if self.n else None

    @property
    def cer(self) -> float | None:
        return self.cer_e / self.cer_n if self.cer_n else None

    def counts(self) -> dict[str, Any]:
        return {"n": self.n, "s": self.s, "d": self.d, "i": self.i, "h": self.h, "errors": self.errors,
                "wer": self.wer, "cer": self.cer, "cer_n": self.cer_n, "cer_e": self.cer_e}

    def ops_json(self) -> list[dict[str, Any]]:
        return [asdict(o) for o in self.ops]


_TYPE = {"equal": "eq", "substitute": "sub", "delete": "del", "insert": "ins"}


def align(ref_tokens: list[str], hyp_tokens: list[str]) -> WerResult:
    """Word-level alignment of two token lists."""
    if not ref_tokens:
        ops = [AlignOp("ins", None, w, None, j) for j, w in enumerate(hyp_tokens)]
        return WerResult(0, 0, 0, len(hyp_tokens), 0, 0, 0, ops)
    if not hyp_tokens:
        ops = [AlignOp("del", w, None, i, None) for i, w in enumerate(ref_tokens)]
        return WerResult(len(ref_tokens), 0, len(ref_tokens), 0, 0, 0, 0, ops)
    out = jiwer.process_words(" ".join(ref_tokens), " ".join(hyp_tokens))  # tokens hold no whitespace
    ops: list[AlignOp] = []
    for chunk in out.alignments[0]:
        t = _TYPE[chunk.type]
        if t in ("eq", "sub"):
            for k in range(chunk.ref_end_idx - chunk.ref_start_idx):
                ri, hi = chunk.ref_start_idx + k, chunk.hyp_start_idx + k
                ops.append(AlignOp(t, ref_tokens[ri], hyp_tokens[hi], ri, hi))
        elif t == "del":
            for ri in range(chunk.ref_start_idx, chunk.ref_end_idx):
                ops.append(AlignOp("del", ref_tokens[ri], None, ri, None))
        else:
            for hi in range(chunk.hyp_start_idx, chunk.hyp_end_idx):
                ops.append(AlignOp("ins", None, hyp_tokens[hi], None, hi))
    return WerResult(len(ref_tokens), out.substitutions, out.deletions, out.insertions, out.hits, 0, 0, ops)


def score(ref: str, hyp: str) -> WerResult:
    """WER and CER for one normalised (reference, hypothesis) pair."""
    r = align(ref.split(), hyp.split())
    if ref:
        if hyp:
            c = jiwer.process_characters(ref, hyp)
            r.cer_n, r.cer_e = len(ref), c.substitutions + c.deletions + c.insertions
        else:
            r.cer_n, r.cer_e = len(ref), len(ref)
    else:
        r.cer_e = len(hyp)
    return r


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Corpus totals from per-utterance `counts()` dicts."""
    tot = {k: sum(r.get(k, 0) or 0 for r in rows) for k in ("n", "s", "d", "i", "h", "cer_n", "cer_e")}
    errors = tot["s"] + tot["d"] + tot["i"]
    tot["errors"] = errors
    tot["wer"] = errors / tot["n"] if tot["n"] else None
    tot["cer"] = tot["cer_e"] / tot["cer_n"] if tot["cer_n"] else None
    tot["utterances"] = len(rows)
    tot["with_errors"] = sum(1 for r in rows if r.get("errors"))
    return tot
