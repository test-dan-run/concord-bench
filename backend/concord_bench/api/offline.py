"""Export for offline: the platform registry with sizes, a preview,
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
from ..offline import export
from ..runs import jobs
from .datasets import job_brief

router = APIRouter(prefix="/offline", tags=["offline"])

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$"


def _dest(dest: str | None) -> Path:
    return Path(dest).expanduser() if dest else paths.offline_kits()


def _names(services: str) -> list[str]:
    return [n.strip() for n in services.split(",") if n.strip()]


def _apps(apps: str | None) -> dict[str, str | None] | None:
    """`concord-bench=concord-bench:0.2.0,concord-label` -> {app: image or None (its default)}; absent: None."""
    if apps is None:
        return None
    out: dict[str, str | None] = {}
    for part in (p.strip() for p in apps.split(",")):
        if part:
            app, _, image = part.partition("=")
            out[app.strip()] = image.strip() or None
    return out


def export_json(rec: OfflineExport, job: Job | None = None) -> dict[str, Any]:
    return {"id": rec.id, "kind": rec.kind, "services": rec.services, "concord_image": rec.concord_image,
            "app_images": rec.concord_image.split(", ") if rec.concord_image else [],
            "dest": rec.dest, "name": rec.name, "status": rec.status, "path": rec.path, "bytes": rec.bytes,
            "error": rec.error, "job_id": rec.job_id, "job": job_brief(job) if job else None,
            "created_at": rec.created_at.isoformat(),
            "finished_at": rec.finished_at.isoformat() if rec.finished_at else None}


@router.get("/services")
def services_table(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Every service in the platform registry with image and weight sizes, used by Bench or not."""
    try:
        rep = export.dry_run()
    except export.ExportError as exc:
        raise HTTPException(503, f"platform unavailable: {exc}") from None
    registered = {m.catalog_id: m for m in session.scalars(select(Model))}
    rows = []
    for r in rep["services"]:
        m = registered.get(r["name"])
        rows.append({**r, "model_id": m.id if m else None, "display_name": m.display_name if m else None})
    return {"services": rows, "platform": rep["platform"], "warnings": rep["warnings"], "apps": export.app_choices(),
            "default_dest": str(paths.offline_kits())}


def _preview(services: list[str], kind: str, dest: Path, apps: dict[str, str | None] | None,
             name: str | None) -> dict[str, Any]:
    try:
        return export.preview(services, kind, dest, apps, name)
    except export.ExportError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/preview")
def preview(services: str, kind: str = "full", dest: str | None = None, apps: str | None = None,
            name: str | None = None) -> dict[str, Any]:
    """`apps`: the apps of a full kit as `app=image` (or a bare app name for its default image), comma-separated;
    absent: this app only."""
    return _preview(_names(services), kind, _dest(dest), _apps(apps), name)


class ExportIn(BaseModel):
    services: list[str] = Field(min_length=1)
    kind: str = "full"  # full | models
    dest: str | None = None
    # The apps of a full kit and their images (null image: the app's default); absent: this app only.
    apps: dict[str, str | None] | None = None
    name: str | None = Field(default=None, pattern=NAME_PATTERN)


@router.post("/exports", status_code=202)
def create_export(body: ExportIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    dest = _dest(body.dest)
    pv = _preview(body.services, body.kind, dest, body.apps, body.name)
    if pv["problems"]:
        raise HTTPException(422, "; ".join(pv["problems"]))
    apps = {a["app"]: a["image"] for a in pv["apps"]}
    rec = OfflineExport(kind=body.kind, services=list(dict.fromkeys(body.services)),
                        concord_image=", ".join(apps.values()) or None, dest=str(dest), name=body.name)
    session.add(rec)
    session.flush()
    job = jobs.enqueue(session, "export", {"export_id": rec.id, "services": rec.services, "kind": rec.kind,
                                           "dest": rec.dest, "apps": apps, "name": rec.name})
    rec.job_id = job.id
    session.commit()
    return {"export": export_json(rec, job), "preview": pv}


@router.get("/exports")
def history(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    recs = list(session.scalars(select(OfflineExport).order_by(OfflineExport.id.desc()).limit(100)))
    by_id = {j.id: j for j in session.scalars(select(Job).where(Job.id.in_([r.job_id for r in recs if r.job_id])))}
    return [export_json(r, by_id.get(r.job_id)) for r in recs]
