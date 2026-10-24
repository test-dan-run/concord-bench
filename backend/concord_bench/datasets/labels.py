"""Labels for slicing (EVALUATE_PLAN.md §7.1): the extra fields that came with the references.

A label's kind follows from its values:
- discrete: text or true / false, or numbers with at most MAX_DISCRETE different values;
- continuous: numbers with more different values than that;
- not used: a different value on almost every record (an ID), long free text, or too many text values to
  slice by. The upload page shows these with the reason.

Numbers written as text (CSV) count as numbers. A label "applies to" the file when every record carrying it
covers the whole file, else to the utterance (segment) it came on.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

INTERNAL = {"source_uid"}  # keys Bench itself keeps in segment.meta, never labels
MAX_DISCRETE = 20
ID_SHARE = 0.9  # this share of records with distinct values (and more than MAX_DISCRETE of them) is an ID
FREE_TEXT_CHARS = 40  # mean length of a text value above which it is free text
TOP_VALUES = 12


def of(meta: dict[str, Any] | None) -> dict[str, Any]:
    """The labels in a segment's or file's meta (Bench's own keys left out)."""
    return {k: v for k, v in (meta or {}).items() if k not in INTERNAL}


def number(v: Any) -> float | None:
    """`v` as a number, or None (true / false are not numbers)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def kind_of(values: list[Any]) -> tuple[str, str | None]:
    """(discrete | continuous | unused, why unused)."""
    distinct = len({str(v) for v in values})
    nums = [number(v) for v in values]
    if values and all(n is not None for n in nums):
        if distinct <= MAX_DISCRETE:
            return "discrete", None
        if all(float(n).is_integer() for n in nums) and distinct >= ID_SHARE * len(values):
            return "unused", f"{distinct} different values: looks like an ID"
        return "continuous", None
    if distinct > MAX_DISCRETE and distinct >= ID_SHARE * len(values):
        return "unused", f"{distinct} different values: looks like an ID"
    if statistics.mean(len(str(v)) for v in values) > FREE_TEXT_CHARS:
        return "unused", "free text"
    if distinct > MAX_DISCRETE:
        return "unused", f"{distinct} different values, too many to slice by"
    return "discrete", None


def value_key(kind: str, v: Any) -> Any:
    """The value a slice groups on: a number for continuous labels and for numeric discrete ones, else text."""
    n = number(v)
    if kind == "continuous":
        return n
    if n is not None and not isinstance(v, bool):
        return int(n) if n.is_integer() else n
    return str(v) if not isinstance(v, bool) else v


def summarize(records: list[tuple[dict[str, Any], bool]]) -> list[dict[str, Any]]:
    """Every label of these records, with its kind, values and coverage.

    `records` holds (labels, covers the whole file) per reference record."""
    total = len(records)
    by_key: dict[str, list[Any]] = {}
    whole: dict[str, bool] = {}
    for meta, is_whole in records:
        for k, v in of(meta).items():
            by_key.setdefault(k, []).append(v)
            whole[k] = whole.get(k, True) and is_whole
    out = []
    for k in sorted(by_key):
        vals = by_key[k]
        kind, why = kind_of(vals)
        entry: dict[str, Any] = {"key": k, "kind": kind, "level": "file" if whole[k] else "utterance",
                                 "have": len(vals), "of": total, "distinct": len({str(v) for v in vals})}
        if why:
            entry["reason"] = why
        if kind == "discrete":
            counts = Counter(value_key(kind, v) for v in vals)
            entry["values"] = [{"value": v, "n": n} for v, n in
                               sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:TOP_VALUES]]
            entry["more"] = max(0, len(counts) - TOP_VALUES)
        elif kind == "continuous":
            nums = sorted(number(v) for v in vals)
            entry["range"] = {"min": nums[0], "max": nums[-1], "median": statistics.median(nums)}
        out.append(entry)
    return out


def usable(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [lb for lb in labels if lb["kind"] != "unused"]
