"""Real-time factor: processing seconds / audio seconds, per stage and summed for pipelines.

Processing time is the model call latency recorded in the output cache, so a cache hit reports the
time the call originally took rather than zero.
"""

from __future__ import annotations


def rtf(processing_s: float, audio_s: float) -> float | None:
    return processing_s / audio_s if audio_s > 0 else None


def by_stage(stage_seconds: dict[str, float], audio_s: float) -> dict[str, float | None]:
    out = {stage: rtf(sec, audio_s) for stage, sec in stage_seconds.items()}
    out["total"] = rtf(sum(stage_seconds.values()), audio_s)
    return out
