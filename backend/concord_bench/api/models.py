"""Registry (read-only, from the platform), compatibility checks, and model registration.

Registering a model = pick a registry entry (+ optional display name, tags, derived-from). Protocol,
base URL, route, image and default tags come from the endpoint map; capabilities and timing level
come from the check.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..analysis.suggest import rank_datasets
from ..core.db import get_session
from ..core.models import Dataset, Job, JobStatus, Model, Run
from ..platform import services
from ..registry import reader
from ..runs import jobs
from .datasets import _clean_tags, job_brief

router = APIRouter(tags=["models"])


def _registry_or_503() -> list[dict[str, Any]]:
    try:
        return reader.entries()
    except reader.RegistryUnavailable as exc:
        raise HTTPException(503, f"platform registry unavailable: {exc}") from None


@router.get("/registry")
def list_registry(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    registered = {m.registry_name: m for m in session.scalars(select(Model))}
    out = []
    for e in _registry_or_503():
        m = registered.get(e["name"])
        out.append({**reader.public(e), "model_id": m.id if m else None,
                    "display_name": m.display_name if m else None})
    return out


@router.get("/registry/status")
def registry_status() -> dict[str, dict[str, str]]:
    """Live /health per entry: up / loading / down (replay in recorded mode)."""
    names = [e["name"] for e in _registry_or_503()]
    with ThreadPoolExecutor(max_workers=16) as pool:
        res = list(pool.map(services.health, names))
    return {h.name: {"state": h.state, "detail": h.detail} for h in res}


@router.get("/registry/{name}")
def registry_entry(name: str) -> dict[str, Any]:
    try:
        return reader.public(reader.get(name))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None


@router.post("/registry/{name}/check", status_code=202)
def start_check(name: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Queue the compatibility check. The worker starts the service if it is down."""
    try:
        e = reader.get(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    job = jobs.enqueue(session, "check", {"service": e["name"]}, [e["name"]] if e["usable_as"] else [])
    session.commit()
    return job_brief(job)


# ---------------------------------------------------------------- models

class ModelIn(BaseModel):
    registry_name: str
    check_job_id: int
    display_name: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = None
    parent_model_id: int | None = None


def model_json(session: Session, m: Model) -> dict[str, Any]:
    return {"id": m.id, "registry_name": m.registry_name, "display_name": m.display_name, "tags": m.tags,
            "parent_model_id": m.parent_model_id, "capabilities": m.capabilities, "timing_level": m.timing_level,
            "check_report": m.check_report, "registry": m.registry_snapshot, "created_at": m.created_at.isoformat(),
            "stages": (["seg"] if m.is_seg else []) + (["asr"] if m.is_asr else []),
            "runs": session.scalar(select(func.count(Run.id)).where(Run.model_id == m.id))}


@router.post("/models", status_code=201)
def create_model(body: ModelIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        e = reader.get(body.registry_name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    if session.scalar(select(Model).where(Model.registry_name == e["name"])):
        raise HTTPException(409, f"{e['name']} is already registered")
    job = session.get(Job, body.check_job_id)
    if job is None or job.type != "check" or job.payload.get("service") != e["name"]:
        raise HTTPException(400, "check_job_id must be a compatibility check of this registry entry")
    if job.status != JobStatus.done:
        raise HTTPException(409, f"the compatibility check is {job.status}")
    report = job.result.get("report") or {}
    if not report.get("capabilities"):
        raise HTTPException(422, "no usable capability was detected, so this model cannot be added")
    if body.parent_model_id is not None and session.get(Model, body.parent_model_id) is None:
        raise HTTPException(400, "derived-from model not found")
    m = Model(registry_name=e["name"], display_name=(body.display_name or "").strip() or e["name"],
              tags=_clean_tags(body.tags if body.tags is not None else list(e.get("tasks") or [])),
              parent_model_id=body.parent_model_id, capabilities=report["capabilities"],
              timing_level=report.get("timing_level"), check_report=report, registry_snapshot=reader.snapshot(e))
    session.add(m)
    session.commit()
    return model_json(session, m)


@router.get("/models")
def list_models(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [model_json(session, m) for m in session.scalars(select(Model).order_by(Model.display_name))]


def get_model(session: Session, model_id: int) -> Model:
    m = session.get(Model, model_id)
    if m is None:
        raise HTTPException(404, "model not found")
    return m


@router.get("/models/{model_id}")
def model_detail(model_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return model_json(session, get_model(session, model_id))


class ModelPatch(BaseModel):
    display_name: str | None = None
    tags: list[str] | None = None
    parent_model_id: int | None = None


@router.patch("/models/{model_id}")
def patch_model(model_id: int, body: ModelPatch, session: Session = Depends(get_session)) -> dict[str, Any]:
    m = get_model(session, model_id)
    data = body.model_dump(exclude_unset=True)
    if "tags" in data:
        data["tags"] = _clean_tags(data["tags"] or [])
    if data.get("parent_model_id") == m.id:
        raise HTTPException(400, "a model cannot be derived from itself")
    for k, v in data.items():
        setattr(m, k, v)
    session.commit()
    return model_json(session, m)


@router.delete("/models/{model_id}", status_code=204)
def delete_model(model_id: int, session: Session = Depends(get_session)) -> None:
    session.delete(get_model(session, model_id))
    session.commit()


@router.post("/models/{model_id}/recheck", status_code=202)
def recheck(model_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    m = get_model(session, model_id)
    job = jobs.enqueue(session, "check", {"service": m.registry_name, "update_model": True}, [m.registry_name])
    session.commit()
    return job_brief(job)


@router.get("/suggestions/datasets")
def suggest_datasets(capabilities: str = "", tags: str = "", session: Session = Depends(get_session)) -> list:
    """Datasets to benchmark a (new) model on: labels for its tasks, ranked by shared tags."""
    caps = [c for c in capabilities.split(",") if c]
    tag_list = [t for t in tags.split(",") if t]
    return rank_datasets(tag_list, caps, list(session.scalars(select(Dataset))))
