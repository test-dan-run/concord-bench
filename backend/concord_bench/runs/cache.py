"""The inference cache (`model_output`), keyed on (model, audio sha256, span, params).

Every model call goes through `get_or_run`, so each seg model runs once per file, each ASR model once
per segment set, and pipelines reuse the single-model rows. The pinned image tag is part of the params,
so a new image means new outputs.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..adapters.base import Adapter, AudioRef
from ..core.models import ModelOutput

WHOLE = -1.0


def _span(start: float, end: float) -> tuple[float, float]:
    return round(start, 3), round(end, 3)


def lookup(session: Session, service: str, sha256: str, start: float, end: float, phash: str) -> ModelOutput | None:
    s, e = _span(start, end)
    return session.scalar(select(ModelOutput).where(
        ModelOutput.model_name == service, ModelOutput.audio_sha256 == sha256, ModelOutput.span_start == s,
        ModelOutput.span_end == e, ModelOutput.params_hash == phash))


def get_or_run(session: Session, adapter: Adapter, audio: np.ndarray, sha256: str, start: float = 0.0,
               end: float = WHOLE, image: str | None = None) -> tuple[ModelOutput, bool]:
    """(cache row, hit?). `audio` is the exact samples to send (already cut for a span)."""
    s, e = _span(start, end)
    phash = adapter.params_hash
    row = lookup(session, adapter.service, sha256, s, e, phash)
    if row is not None:
        return row, True
    output, latency = adapter.run(audio, AudioRef(sha256, s, e))
    row = ModelOutput(model_name=adapter.service, audio_sha256=sha256, span_start=s, span_end=e, params_hash=phash,
                      output=output, latency_s=latency, image=image)
    try:
        with session.begin_nested():  # savepoint: losing a race must not undo the caller's work
            session.add(row)
    except IntegrityError:  # another process cached it first
        row = lookup(session, adapter.service, sha256, s, e, phash)
        assert row is not None
        return row, True
    return row, False


def outputs_for(session: Session, ids: list[int]) -> dict[int, ModelOutput]:
    if not ids:
        return {}
    return {r.id: r for r in session.scalars(select(ModelOutput).where(ModelOutput.id.in_(ids)))}


def as_dict(row: ModelOutput) -> dict[str, Any]:
    return {"id": row.id, "model": row.model_name, "span": [row.span_start, row.span_end], "output": row.output,
            "latency_s": row.latency_s}
