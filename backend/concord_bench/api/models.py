"""Models: the model catalog's speech models, read-only. Registration, verification and the model
contract live in the catalog's own UI (platform/catalog/README.md); Bench keeps a row per model it has
used (runs and pipelines point at it), refreshed from the catalog's record whenever models are listed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.db import get_session
from ..core.models import Model, Run
from ..core.settings import get_settings
from ..platform import catalog

router = APIRouter(tags=["models"])


def catalog_entry(rec: dict[str, Any] | None, m: Model | None, runs: int = 0) -> dict[str, Any]:
    """A model as pickers and the Models page show it: the live record when the catalog answered, else the
    last record Bench kept."""
    src = rec if rec is not None else (m.catalog if m is not None else {})
    caps = (rec.get("capabilities") if rec is not None else src.get("capabilities")) or {}
    tasks = [t for t in caps.get("tasks") or [] if t in ("asr", "vad", "diar", "kws")]
    cid = rec["id"] if rec is not None else m.catalog_id
    why = catalog.unusable_reason(rec, cid) if rec is not None else "not in the model catalog (or it is unreachable)"
    return {
        "catalog_id": cid, "model_id": m.id if m else None, "in_catalog": rec is not None,
        "display_name": (rec or {}).get("display_name") or (m.display_name if m else cid),
        "description": src.get("description"), "tags": list((rec or {}).get("tags") or (m.tags if m else [])),
        "parent_id": src.get("parent_id"), "contract": src.get("contract"), "api": src.get("api"),
        "engine": src.get("engine"), "image": src.get("image"), "build_key": src.get("build_key"),
        "state": (rec or {}).get("state"), "state_detail": (rec or {}).get("state_detail"),
        "leased_by": (rec or {}).get("leased_by") or [],
        "capabilities": tasks, "capability_status": caps.get("status"), "usable_as": caps.get("usable_as"),
        "timing_level": caps.get("timing_level") if "asr" in tasks else None,
        "output_detail": caps.get("output_detail") or [], "rtf": caps.get("rtf"), "verified_at": caps.get("verified_at"),
        "has_speakers": "diar" in tasks,
        "stages": (["seg"] if {"vad", "diar"} & set(tasks) else []) + (["asr"] if "asr" in tasks else []),
        "cold_start_s": (rec or {}).get("cold_start_s"), "cold_start_measured": bool((rec or {}).get("cold_start_measured")),
        "gpu": (rec or {}).get("gpu"), "usable": why is None, "not_usable": why, "runs": runs,
    }


def catalog_info(error: str | None = None) -> dict[str, Any]:
    s = get_settings()
    return {"url": s.catalog_url.rstrip("/"), "ui_url": s.catalog_ui, "available": error is None, "error": error}


def catalog_listing(session: Session) -> dict[str, Any]:
    """The catalog's speech models (state, capabilities, cold start, build key), then models Bench has runs
    for that the catalog no longer lists. One catalog call (cached a few seconds), not one per model."""
    rows = {m.catalog_id: m for m in session.scalars(select(Model))}
    runs = dict(session.execute(select(Run.model_id, func.count(Run.id)).group_by(Run.model_id)).all())
    try:
        recs = catalog.records()
    except catalog.CatalogUnavailable as exc:
        entries = [catalog_entry(None, m, runs.get(m.id, 0)) for m in rows.values()]
        return {"models": sorted(entries, key=lambda e: e["display_name"].lower()), "catalog": catalog_info(str(exc))}
    catalog.sync_models(session, recs)
    entries = [catalog_entry(r, rows.get(r["id"]), runs.get(rows[r["id"]].id, 0) if r["id"] in rows else 0)
               for r in recs.values() if catalog.speech_model(r)]
    listed = {e["catalog_id"] for e in entries}
    entries += [catalog_entry(recs.get(cid), m, runs.get(m.id, 0)) for cid, m in rows.items() if cid not in listed]
    return {"models": sorted(entries, key=lambda e: (not e["in_catalog"], e["display_name"].lower())),
            "catalog": catalog_info()}


@router.get("/models")
def list_models(session: Session = Depends(get_session)) -> dict[str, Any]:
    return catalog_listing(session)


@router.get("/models/{model_id}")
def model_detail(model_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    m = session.get(Model, model_id)
    if m is None:
        raise HTTPException(404, "model not found")
    try:
        rec = catalog.record(m.catalog_id)
    except catalog.CatalogUnavailable:
        rec = None
    runs = session.scalar(select(func.count(Run.id)).where(Run.model_id == m.id)) or 0
    return catalog_entry(rec, m, runs)
