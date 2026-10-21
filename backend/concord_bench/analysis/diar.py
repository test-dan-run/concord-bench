"""VAD / diarisation analysis cards: error by condition, turns found by length, boundary offsets."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from ..core.models import Result

TURN_BUCKETS = [(0, 1, "< 1 s"), (1, 2, "1–2 s"), (2, 5, "2–5 s"), (5, 1e9, "5 s +")]


def _err(rows: list[Result]) -> dict[str, Any]:
    keys = ("missed", "false_alarm", "confusion", "total", "vad_missed", "vad_false_alarm", "vad_total")
    t = {k: sum((r.metrics or {}).get(k) or 0.0 for r in rows) for k in keys}
    der = (t["missed"] + t["false_alarm"] + t["confusion"]) / t["total"] if t["total"] else None
    vad = (t["vad_missed"] + t["vad_false_alarm"]) / t["vad_total"] if t["vad_total"] else None
    return {"files": len(rows), "der": der, "vad_error": vad, **t}


def by_condition(results: list[Result], meta_by_file: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    keys: dict[str, set] = defaultdict(set)
    for meta in meta_by_file.values():
        for k, v in (meta or {}).items():
            if isinstance(v, (str, int, bool)):
                keys[k].add(v)
    out = []
    for k, vals in sorted(keys.items()):
        if not 2 <= len(vals) <= 20:
            continue
        groups: dict[Any, list[Result]] = defaultdict(list)
        for r in results:
            v = (meta_by_file.get(r.audio_file_id) or {}).get(k)
            if v is not None:
                groups[v].append(r)
        out += [{"group": k, "label": str(v), **_err(rs)} for v, rs in sorted(groups.items(), key=lambda kv: str(kv[0]))]
    return out


def turns_found(results: list[Result]) -> list[dict[str, Any]]:
    counts = {lbl: [0, 0] for _, _, lbl in TURN_BUCKETS}
    for r in results:
        for dur, found in (r.alignment or {}).get("turns_found", []):
            for lo, hi, lbl in TURN_BUCKETS:
                if lo <= dur < hi:
                    counts[lbl][0] += 1
                    counts[lbl][1] += bool(found)
                    break
    return [{"label": lbl, "turns": n, "found": f, "rate": f / n if n else None} for lbl, (n, f) in counts.items()]


def boundary_histogram(results: list[Result], width: float = 0.1, span: float = 1.0) -> dict[str, Any]:
    offs = [o for r in results for o in (r.alignment or {}).get("boundary_offsets", [])]
    edges = np.round(np.arange(-span, span + width / 2, width), 3)
    hist, _ = np.histogram(offs, bins=edges) if offs else (np.zeros(len(edges) - 1, dtype=int), edges)
    return {"edges": edges.tolist(), "counts": hist.tolist(), "n": len(offs),
            "median_abs": float(np.median(np.abs(offs))) if offs else None}


def largest_errors(alignment: dict[str, Any], top: int = 6) -> list[dict[str, Any]]:
    return sorted(alignment.get("errors", []), key=lambda e: -e["dur"])[:top]
