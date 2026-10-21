"""VAD metrics: missed and false-alarm speech, speaker-agnostic (pyannote DetectionErrorRate).

Same collar convention as `der`: `collar` seconds on each side of every reference boundary.
"""

from __future__ import annotations

import warnings
from typing import Any

from .der import DEFAULT_COLLAR, Turn, annotation, uem


def detection(ref: list[Turn], hyp: list[Turn], duration: float, collar: float = DEFAULT_COLLAR) -> dict[str, Any]:
    from pyannote.metrics.detection import DetectionErrorRate

    r = annotation(Turn(t.start, t.end, "speech") for t in ref)
    h = annotation(Turn(t.start, t.end, "speech") for t in hyp)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        comp = DetectionErrorRate(collar=2 * collar)(r, h, uem=uem(duration), detailed=True)
    total = comp["total"]
    return {
        "vad_missed": comp["miss"], "vad_false_alarm": comp["false alarm"], "vad_total": total,
        "vad_error": comp["detection error rate"] if total else None,
        "vad_missed_rate": comp["miss"] / total if total else None,
        "vad_false_alarm_rate": comp["false alarm"] / total if total else None,
        "speech_ref": r.get_timeline().support().duration(),
        "speech_hyp": h.get_timeline().support().duration(),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tot = {k: sum(r.get(k) or 0.0 for r in rows) for k in ("vad_missed", "vad_false_alarm", "vad_total")}
    t = tot["vad_total"]
    tot["vad_error"] = (tot["vad_missed"] + tot["vad_false_alarm"]) / t if t else None
    tot["vad_missed_rate"] = tot["vad_missed"] / t if t else None
    tot["vad_false_alarm_rate"] = tot["vad_false_alarm"] / t if t else None
    tot["files"] = len(rows)
    return tot
