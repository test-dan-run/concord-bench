"""Export for offline (OFFLINE_EXPORT_PLAN.md §5): the platform registry with sizes, a preview,
export jobs and their history. Kits are written to a folder on this machine; nothing goes through
the browser."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import paths
from ..core.db import get_session
from ..core.models import Job, Model, OfflineExport
from ..core.settings import get_settings
from ..offline import export
from ..runs import jobs
from .datasets import job_brief

router = APIRouter(prefix="/offline", tags=["offline"])

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$"


def _dest(dest: str | None) -> Path:
    return Path(dest).expanduser() if dest else paths.offline_kits()


def _names(services: str) -> list[str]:
    return [n.strip() for n in services.split(",") if n.strip()]


def export_json(rec: OfflineExport, job: Job | None = None) -> dict[str, Any]:
    return {"id": rec.id, "kind": rec.kind, "services": rec.services, "concord_image": rec.concord_image,
            "dest": rec.dest, "name": rec.name, "status": rec.status, "path": rec.path, "bytes": rec.bytes,
            "error": rec.error, "job_id": rec.job_id, "job": job_brief(job) if job else None,
            "created_at": rec.created_at.isoformat(),
            "finished_at": rec.finished_at.isoformat() if rec.finished_at else None}


@router.get("/services")
def services_table(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Every service in the platform registry with image and weight sizes, registered in Concord or not."""
    try:
        rep = export.dry_run()
    except export.ExportError as exc:
        raise HTTPException(503, f"platform unavailable: {exc}") from None
    registered = {m.registry_name: m for m in session.scalars(select(Model))}
    rows = []
    for r in rep["services"]:
        m = registered.get(r["name"])
        rows.append({**r, "model_id": m.id if m else None, "display_name": m.display_name if m else None})
    images = []
    for ref in export.concord_images():
        facts = export.image_facts(ref)
        images.append({"image": ref, "present": facts is not None, "bytes": facts["bytes"] if facts else None})
    return {"services": rows, "platform": rep["platform"], "warnings": rep["warnings"], "concord_images": images,
            "default_image": get_settings().concord_image, "default_dest": str(paths.offline_kits())}


@router.get("/preview")
def preview(services: str, kind: str = "full", dest: str | None = None, concord_image: str | None = None,
            name: str | None = None) -> dict[str, Any]:
    try:
        return export.preview(_names(services), kind, _dest(dest), concord_image, name)
    except export.ExportError as exc:
        raise HTTPException(422, str(exc)) from None


class ExportIn(BaseModel):
    services: list[str] = Field(min_length=1)
    kind: str = "full"  # full | models
    dest: str | None = None
    concord_image: str | None = None
    name: str | None = Field(default=None, pattern=NAME_PATTERN)


@router.post("/exports", status_code=202)
def create_export(body: ExportIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    dest = _dest(body.dest)
    pv = preview(",".join(body.services), body.kind, str(dest), body.concord_image, body.name)
    if pv["problems"]:
        raise HTTPException(422, "; ".join(pv["problems"]))
    image = pv["concord"]["image"] if pv["concord"] else None
    rec = OfflineExport(kind=body.kind, services=list(dict.fromkeys(body.services)), concord_image=image,
                        dest=str(dest), name=body.name)
    session.add(rec)
    session.flush()
    job = jobs.enqueue(session, "export", {"export_id": rec.id, "services": rec.services, "kind": rec.kind,
                                           "dest": rec.dest, "concord_image": image, "name": rec.name})
    rec.job_id = job.id
    session.commit()
    return {"export": export_json(rec, job), "preview": pv}


@router.get("/exports")
def history(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    recs = list(session.scalars(select(OfflineExport).order_by(OfflineExport.id.desc()).limit(100)))
    by_id = {j.id: j for j in session.scalars(select(Job).where(Job.id.in_([r.job_id for r in recs if r.job_id])))}
    return [export_json(r, by_id.get(r.job_id)) for r in recs]
