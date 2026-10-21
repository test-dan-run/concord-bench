"""Pipeline error attribution (Q8).

For one (seg model, ASR model, file):
- A = cpWER(oracle segments + oracle speakers -> ASR)
- B = cpWER(model segments, each given the reference speaker it overlaps most -> ASR)
- C = cpWER(model segments + model speakers -> ASR), the reported score

ASR = A, segmentation = B - A, speaker attribution = C - B. The "with reference segments"
counterfactual is A. All three share the reference word count N, so the split also holds on
error counts, and corpus values are summed errors / summed N.

Per-file components can be negative (B < A when model segments happen to suit the ASR better);
tables show them raw, charts clip them at 0.
"""

from __future__ import annotations

from typing import Any


def split(a_errors: int, b_errors: int, c_errors: int, n: int) -> dict[str, Any]:
    def rate(x: float) -> float | None:
        return x / n if n else None

    return {
        "n": n, "a_errors": a_errors, "b_errors": b_errors, "c_errors": c_errors,
        "asr": rate(a_errors), "segmentation": rate(b_errors - a_errors), "speaker": rate(c_errors - b_errors),
        "cpwer": rate(c_errors), "with_reference_segments": rate(a_errors),
        "asr_errors": a_errors, "segmentation_errors": b_errors - a_errors, "speaker_errors": c_errors - b_errors,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    a = sum(r["a_errors"] for r in rows)
    b = sum(r["b_errors"] for r in rows)
    c = sum(r["c_errors"] for r in rows)
    n = sum(r["n"] for r in rows)
    out = split(a, b, c, n)
    out["files"] = len(rows)
    return out


def clipped(parts: dict[str, Any]) -> dict[str, float]:
    """For charts: negatives -> 0."""
    return {k: max(0.0, parts.get(k) or 0.0) for k in ("asr", "segmentation", "speaker")}


def best_ref_speaker(start: float, end: float, ref_turns: list[tuple[float, float, str]]) -> str | None:
    """The reference speaker overlapping [start, end] the most (B's speaker assignment)."""
    best, best_ov = None, 0.0
    totals: dict[str, float] = {}
    for rs, re_, spk in ref_turns:
        ov = min(end, re_) - max(start, rs)
        if ov > 0:
            totals[spk] = totals.get(spk, 0.0) + ov
    for spk, ov in totals.items():
        if ov > best_ov:
            best, best_ov = spk, ov
    return best
