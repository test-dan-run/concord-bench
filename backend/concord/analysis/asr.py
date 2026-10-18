"""ASR analysis below the utterance table: most mis-transcribed words, top substitution pairs, WER by slice."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ..core.models import Result

DURATION_BUCKETS = [(0, 2, "< 2 s"), (2, 5, "2–5 s"), (5, 10, "5–10 s"), (10, 30, "10–30 s"), (30, 1e9, "30 s +")]


def word_errors(results: list[Result], top: int = 12) -> list[dict[str, Any]]:
    """Reference words ranked by how often they were got wrong (substituted or deleted)."""
    seen: Counter[str] = Counter()
    wrong: Counter[str] = Counter()
    became: dict[str, Counter[str]] = defaultdict(Counter)
    for r in results:
        for op in (r.alignment or {}).get("ops", []):
            w = op.get("ref")
            if w is None:
                continue
            seen[w] += 1
            if op["op"] == "sub":
                wrong[w] += 1
                became[w][op["hyp"]] += 1
            elif op["op"] == "del":
                wrong[w] += 1
                became[w]["(deleted)"] += 1
    rows = []
    for w, n in wrong.most_common():
        top_hyp, top_n = became[w].most_common(1)[0]
        rows.append({"word": w, "errors": n, "occurrences": seen[w], "rate": n / seen[w], "top": top_hyp,
                     "top_n": top_n})
    rows.sort(key=lambda r: (-r["errors"], -r["rate"], r["word"]))
    return rows[:top]


def substitution_pairs(results: list[Result], top: int = 12) -> list[dict[str, Any]]:
    c: Counter[tuple[str, str]] = Counter()
    for r in results:
        for op in (r.alignment or {}).get("ops", []):
            if op["op"] == "sub":
                c[(op["ref"], op["hyp"])] += 1
    out = []
    for (ref, hyp), n in c.most_common(top):
        note = "spelling / form" if _similar(ref, hyp) else ""
        out.append({"ref": ref, "hyp": hyp, "n": n, "note": note})
    return out


def _similar(a: str, b: str) -> bool:
    if a.replace("'", "") == b.replace("'", ""):
        return True
    if a.isdigit() != b.isdigit():
        return True
    common = sum(1 for x, y in zip(a, b, strict=False) if x == y)
    return common >= max(3, int(0.6 * max(len(a), len(b))))


def _corpus(rows: list[Result]) -> dict[str, Any]:
    n = sum((r.metrics or {}).get("n", 0) for r in rows)
    e = sum((r.metrics or {}).get("errors", 0) for r in rows)
    return {"n": n, "errors": e, "wer": e / n if n else None, "utterances": len(rows)}


def slices(results: list[Result], meta_by_file: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """WER by file metadata (keys with 2-20 values), duration bucket and speaker."""
    out: list[dict[str, Any]] = []
    keys: dict[str, set] = defaultdict(set)
    for meta in meta_by_file.values():
        for k, v in (meta or {}).items():
            if isinstance(v, (str, int, bool)):
                keys[k].add(v)
    for k, vals in sorted(keys.items()):
        if not 2 <= len(vals) <= 20:
            continue
        groups: dict[Any, list[Result]] = defaultdict(list)
        for r in results:
            v = (meta_by_file.get(r.audio_file_id) or {}).get(k)
            if v is not None:
                groups[v].append(r)
        out += [{"group": k, "label": str(v), **_corpus(rs)} for v, rs in sorted(groups.items(), key=lambda kv: str(kv[0]))]
    buckets: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        d = (r.end or 0) - (r.start or 0)
        for lo, hi, lbl in DURATION_BUCKETS:
            if lo <= d < hi:
                buckets[lbl].append(r)
                break
    if len(buckets) > 1:
        out += [{"group": "duration", "label": lbl, **_corpus(buckets[lbl])} for _, _, lbl in DURATION_BUCKETS
                if lbl in buckets]
    spk: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        if r.speaker:
            spk[r.speaker].append(r)
    if len(spk) > 1:
        out += [{"group": "speaker", "label": s, **_corpus(rs)} for s, rs in sorted(spk.items())]
    return out


def has_error_word(r: Result, word: str) -> bool:
    return any(op.get("ref") == word and op["op"] in ("sub", "del") for op in (r.alignment or {}).get("ops", []))
