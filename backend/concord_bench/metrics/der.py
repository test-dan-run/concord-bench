"""DER / JER (pyannote.metrics), speaker mapping, and error regions for the timeline lanes.

Collar convention: Concord's `collar` is the NIST one, seconds forgiven on **each side** of every
reference boundary (default 0.25 s). pyannote's `collar` is the total width, so it gets `2 * collar`.
The scoring region (UEM) is the whole file, [0, duration].
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

DEFAULT_COLLAR = 0.25
FRAME = 0.01  # error-lane resolution, seconds


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: str

    @property
    def dur(self) -> float:
        return self.end - self.start


def annotation(turns: Iterable[Turn]):
    from pyannote.core import Annotation, Segment

    ann = Annotation()
    for k, t in enumerate(turns):
        if t.end > t.start:
            ann[Segment(t.start, t.end), k] = t.speaker
    return ann


def uem(duration: float):
    from pyannote.core import Segment, Timeline

    return Timeline([Segment(0.0, duration)])


def der(ref: list[Turn], hyp: list[Turn], duration: float, collar: float = DEFAULT_COLLAR,
        skip_overlap: bool = False) -> dict[str, Any]:
    """Missed / false alarm / confusion seconds, DER, JER and the optimal hyp -> ref speaker mapping."""
    from pyannote.metrics.diarization import DiarizationErrorRate, JaccardErrorRate

    r, h, u = annotation(ref), annotation(hyp), uem(duration)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        metric = DiarizationErrorRate(collar=2 * collar, skip_overlap=skip_overlap)
        comp = metric(r, h, uem=u, detailed=True)
        mapping = {str(k): str(v) for k, v in metric.optimal_mapping(r, h).items()} if len(h) and len(r) else {}
        jer = JaccardErrorRate(collar=2 * collar, skip_overlap=skip_overlap)(r, h, uem=u, detailed=True)
    total = comp["total"]
    ref_spk = sorted({t.speaker for t in ref})
    hyp_spk = sorted({t.speaker for t in hyp})
    return {
        "missed": comp["missed detection"], "false_alarm": comp["false alarm"], "confusion": comp["confusion"],
        "total": total, "correct": comp["correct"],
        "der": comp["diarization error rate"] if total else None,
        "jer": jer["jaccard error rate"], "jer_speaker_error": jer["speaker error"], "jer_speakers": jer["speaker count"],
        "mapping": mapping, "ref_speakers": len(ref_spk), "hyp_speakers": len(hyp_spk),
        "speaker_count_ok": len(ref_spk) == len(hyp_spk),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Corpus DER = summed error seconds / summed reference seconds. JER is the mean of file JERs."""
    tot = {k: sum(r.get(k) or 0.0 for r in rows) for k in ("missed", "false_alarm", "confusion", "total", "correct")}
    err = tot["missed"] + tot["false_alarm"] + tot["confusion"]
    tot["der"] = err / tot["total"] if tot["total"] else None
    jers = [r["jer"] for r in rows if r.get("jer") is not None]
    tot["jer"] = float(np.mean(jers)) if jers else None
    tot["speaker_count_acc"] = (sum(1 for r in rows if r.get("speaker_count_ok")) / len(rows)) if rows else None
    tot["files"] = len(rows)
    return tot


def _frames(turns: list[Turn], duration: float, mapping: dict[str, str] | None = None) -> tuple[list[str], np.ndarray]:
    """Boolean [frames, speakers] activity matrix."""
    n = int(np.ceil(duration / FRAME)) + 1
    names = sorted({(mapping or {}).get(t.speaker, t.speaker) for t in turns})
    idx = {s: k for k, s in enumerate(names)}
    act = np.zeros((n, max(1, len(names))), dtype=bool)
    for t in turns:
        s = (mapping or {}).get(t.speaker, t.speaker)
        a, b = max(0, int(round(t.start / FRAME))), min(n, int(round(t.end / FRAME)))
        act[a:b, idx[s]] = True
    return names, act


def error_regions(ref: list[Turn], hyp: list[Turn], duration: float, mapping: dict[str, str],
                  min_dur: float = 0.05) -> list[dict[str, Any]]:
    """Missed / false-alarm / confusion regions at 10 ms resolution, for the error lane.

    Uses the optimal mapping (hyp -> ref), no collar: the lane shows where errors are, the DER
    number comes from pyannote. Unmapped hyp speakers stay distinct.
    """
    hyp_m = [Turn(t.start, t.end, mapping.get(t.speaker, f"hyp:{t.speaker}")) for t in hyp]
    names = sorted({t.speaker for t in ref} | {t.speaker for t in hyp_m})
    _, ra = _frames([Turn(t.start, t.end, t.speaker) for t in ref] + [Turn(0, 0, s) for s in names], duration)
    _, ha = _frames(hyp_m + [Turn(0, 0, s) for s in names], duration)
    nref, nhyp = ra.sum(1), ha.sum(1)
    correct = (ra & ha).sum(1)
    kinds = np.full(len(nref), "", dtype=object)
    missed = nref > nhyp
    fa = nhyp > nref
    conf = (np.minimum(nref, nhyp) - correct) > 0
    kinds[conf] = "confusion"
    kinds[missed & ~conf] = "missed"
    kinds[fa & ~conf] = "false_alarm"
    out: list[dict[str, Any]] = []
    start = None
    for k in range(len(kinds) + 1):
        cur = kinds[k] if k < len(kinds) else ""
        prev = kinds[k - 1] if k > 0 else ""
        if cur != prev:
            if prev and start is not None:
                s, e = start * FRAME, min(duration, k * FRAME)
                if e - s >= min_dur:
                    out.append({"type": prev, "start": round(s, 3), "end": round(e, 3), "dur": round(e - s, 3)})
            start = k if cur else None
    return out


def overlaps(turns: list[Turn], duration: float) -> list[dict[str, float]]:
    """Regions where two or more speakers talk, for the overlap hatch."""
    _, act = _frames(turns, duration)
    many = act.sum(1) >= 2
    out, start = [], None
    for k in range(len(many) + 1):
        cur = bool(many[k]) if k < len(many) else False
        if cur and start is None:
            start = k
        elif not cur and start is not None:
            out.append({"start": round(start * FRAME, 3), "end": round(k * FRAME, 3)})
            start = None
    return out


def boundary_offsets(ref: list[Turn], hyp: list[Turn], max_offset: float = 1.0) -> list[float]:
    """Signed offset (hyp - ref) from each reference boundary to the nearest hypothesis boundary."""
    hb = np.array(sorted({t.start for t in hyp} | {t.end for t in hyp}))
    if not len(hb):
        return []
    out = []
    for b in sorted({t.start for t in ref} | {t.end for t in ref}):
        k = int(np.argmin(np.abs(hb - b)))
        off = float(hb[k] - b)
        if abs(off) <= max_offset:
            out.append(round(off, 3))
    return out
