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


# ---------------------------------------------------------------- EVALUATE_PLAN.md §8.1

FRAME = 0.01
NEAR_CHANGE_S = 0.5
SHORT_TURN_S = 1.0
REGIONS = {"overlap": "Overlapped speech", "near_change": "Within 0.5 s of a speaker change",
           "short": "Turns under 1 s (backchannels)", "nonspeech": "Non-speech"}
ERROR_TYPES = ("missed", "false_alarm", "confusion")


def _moments(ref: list[list[Any]], duration: float) -> dict[str, np.ndarray]:
    """Boolean frame masks of each kind of moment, from the reference turns [start, end, speaker]."""
    n = int(np.ceil(duration / FRAME)) + 1
    count = np.zeros(n, dtype=np.int16)
    short = np.zeros(n, dtype=bool)
    near = np.zeros(n, dtype=bool)
    by_start = sorted(ref, key=lambda t: (t[0], t[1]))
    for s, e, _spk in by_start:
        a, b = max(0, int(round(s / FRAME))), min(n, int(round(e / FRAME)))
        count[a:b] += 1
        if e - s < SHORT_TURN_S:
            short[a:b] = True
    # a speaker change: one speaker's turn ends and another's starts within NEAR_CHANGE_S
    w = int(round(NEAR_CHANGE_S / FRAME))
    for i, (_s, e, spk) in enumerate(by_start):
        for s2, _e2, spk2 in by_start[i + 1:]:
            if s2 - e > NEAR_CHANGE_S:
                break
            if spk2 != spk and abs(s2 - e) <= NEAR_CHANGE_S:
                c = int(round((e + s2) / 2 / FRAME))
                near[max(0, c - w):min(n, c + w)] = True
    return {"overlap": count >= 2, "near_change": near & (count >= 1), "short": short, "nonspeech": count == 0,
            "speech": count >= 1}


def _error_masks(errors: list[dict[str, Any]], n: int) -> dict[str, np.ndarray]:
    out = {k: np.zeros(n, dtype=bool) for k in ERROR_TYPES}
    for e in errors:
        a, b = max(0, int(round(e["start"] / FRAME))), min(n, int(round(e["end"] / FRAME)))
        if e["type"] in out:
            out[e["type"]][a:b] = True
    return out


def context_of(start: float, end: float, masks: dict[str, np.ndarray]) -> list[str]:
    """The kinds of moment an error region falls in (more than half of it)."""
    a, b = int(round(start / FRAME)), max(int(round(start / FRAME)) + 1, int(round(end / FRAME)))
    return [k for k in REGIONS if masks[k][a:b].mean() > 0.5] if b <= len(masks["speech"]) else []


def regions(results: list[Result], files: dict[int, str]) -> dict[str, Any]:
    """Where the errors are: per kind of moment, its share of speech and of error time, the lift, the error
    mix and DER inside it, and its worst moments (EVALUATE_PLAN.md §8.1). Kinds overlap."""
    tot_speech = tot_err = 0.0
    acc = {k: {"speech": 0.0, "time": 0.0, **{t: 0.0 for t in ERROR_TYPES}} for k in REGIONS}
    moments: dict[str, list[dict[str, Any]]] = {k: [] for k in REGIONS}
    for r in results:
        al = r.alignment or {}
        ref, errors = al.get("ref") or [], al.get("errors") or []
        if not ref and not errors:
            continue
        masks = _moments(ref, r.end or 0.0)
        n = len(masks["speech"])
        em = _error_masks(errors, n)
        anyerr = em["missed"] | em["false_alarm"] | em["confusion"]
        tot_speech += masks["speech"].sum() * FRAME
        tot_err += anyerr.sum() * FRAME
        for k in REGIONS:
            m = masks[k]
            acc[k]["speech"] += (m & masks["speech"]).sum() * FRAME
            acc[k]["time"] += m.sum() * FRAME
            for t in ERROR_TYPES:
                acc[k][t] += (m & em[t]).sum() * FRAME
        for e in errors:
            for k in context_of(e["start"], e["end"], masks):
                moments[k].append({"file_id": r.audio_file_id, "file": files.get(r.audio_file_id, r.utterance_key),
                                   "start": e["start"], "end": e["end"], "dur": e["dur"], "type": e["type"]})
    rows = []
    for k, label in REGIONS.items():
        a = acc[k]
        err = sum(a[t] for t in ERROR_TYPES)
        share_speech = a["speech"] / tot_speech if tot_speech else None
        share_err = err / tot_err if tot_err else None
        rows.append({
            "key": k, "label": label, "seconds": a["time"], "speech_s": a["speech"], "error_s": err,
            "share_speech": share_speech, "share_errors": share_err,
            "lift": share_err / share_speech if share_err is not None and share_speech else None,
            "mix": {t: a[t] / err if err else 0.0 for t in ERROR_TYPES},
            "der": err / a["speech"] if a["speech"] else None,
            "worst": sorted(moments[k], key=lambda x: -x["dur"])[:5]})
    return {"rows": rows, "speech_s": tot_speech, "error_s": tot_err}


def speaker_counts(results: list[Result]) -> dict[str, Any]:
    """Files by true speaker count (rows) and the count the model found (columns)."""
    pairs = [((r.metrics or {}).get("ref_speakers"), (r.metrics or {}).get("hyp_speakers")) for r in results]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if not pairs:
        return {"rows": [], "cols": [], "exact": 0, "under": 0, "over": 0, "files": 0}
    true = sorted({a for a, _ in pairs})
    found = sorted({b for _, b in pairs})
    rows = [{"label": t, "cells": [sum(1 for a, b in pairs if a == t and b == f) for f in found]} for t in true]
    return {"rows": rows, "cols": found, "files": len(pairs), "exact": sum(1 for a, b in pairs if a == b),
            "under": sum(1 for a, b in pairs if b < a), "over": sum(1 for a, b in pairs if b > a)}


def confusion(result: Result) -> dict[str, Any]:
    """Seconds of each reference speaker's speech by the model speaker it went to, for one file; plus
    merges (one model speaker holding most of two reference speakers) and splits."""
    al = result.alignment or {}
    ref, hyp = al.get("ref") or [], al.get("hyp") or []
    dur = result.end or max([t[1] for t in ref + hyp] or [0.0])
    n = int(np.ceil(dur / FRAME)) + 1

    def acts(turns: list[list[Any]]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for s, e, spk in turns:
            m = out.setdefault(spk, np.zeros(n, dtype=bool))
            m[max(0, int(round(s / FRAME))):min(n, int(round(e / FRAME)))] = True
        return out

    ra, ha = acts(ref), acts(hyp)
    rs, hs = sorted(ra), sorted(ha)
    none = ~np.any(np.stack(list(ha.values())), axis=0) if ha else np.ones(n, dtype=bool)
    rows = []
    for r in rs:
        talk = ra[r].sum() * FRAME
        cells = [float((ra[r] & ha[h]).sum() * FRAME) for h in hs]
        rows.append({"speaker": r, "talk_s": talk, "cells": cells, "missed_s": float((ra[r] & none).sum() * FRAME)})
    notes = []
    for j, h in enumerate(hs):
        big = [r["speaker"] for r in rows if r["talk_s"] and r["cells"][j] / r["talk_s"] > 0.5]
        if len(big) > 1:
            notes.append({"kind": "merge", "hyp": h, "ref": big})
    for r in rows:
        parts = [hs[j] for j, v in enumerate(r["cells"]) if r["talk_s"] and v / r["talk_s"] > 0.3]
        if len(parts) > 1:
            notes.append({"kind": "split", "ref": [r["speaker"]], "hyp": parts})
    return {"file_id": result.audio_file_id, "cols": hs, "rows": rows, "mapping": al.get("mapping") or {},
            "notes": notes}
