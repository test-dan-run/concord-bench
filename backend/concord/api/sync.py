"""Import / export of labelling progress as label packages (label/sync.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_session
from ..core.models import SyncPackage
from ..label import sync
from .datasets import _local_dir, _upload_dir, get_dataset

router = APIRouter(prefix="/sync", tags=["sync"])


def _pkg(session: Session, package_id: int) -> SyncPackage:
    p = session.get(SyncPackage, package_id)
    if p is None:
        raise HTTPException(404, "package not found")
    return p


def _run(session: Session, fn):
    try:
        out = fn()
    except sync.SyncError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from None
    session.commit()
    return out


@router.get("/packages")
def history(dataset_id: int | None = None, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(SyncPackage).order_by(SyncPackage.id.desc()).limit(200)
    if dataset_id:
        stmt = stmt.where(SyncPackage.dataset_id == dataset_id)
    return [sync.package_json(p) for p in session.scalars(stmt)]


# ---------------------------------------------------------------- export

class ExportIn(BaseModel):
    dataset_id: int
    scope: str = "changed"  # changed | all | files
    file_ids: list[int] = Field(default_factory=list)
    include_pseudo: bool = True
    include_outputs: bool = False
    include_audio: bool = False


@router.get("/exports/preview")
def export_preview(dataset_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = get_dataset(session, dataset_id)
    prev = sync.last_export(session, ds.id)
    changed = None
    if prev is not None:
        changed = sum(1 for f in ds.files if any(s.updated_at > prev.created_at for s in f.segments))
    return {"dataset_id": ds.id, "files": len(ds.files), "last_export": sync.package_json(prev) if prev else None,
            "changed_files": changed}


@router.post("/exports", status_code=201)
def create_export(body: ExportIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = get_dataset(session, body.dataset_id)
    pkg = _run(session, lambda: sync.export(session, ds, body.scope, body.file_ids, body.include_pseudo,
                                            body.include_outputs, body.include_audio))
    return sync.package_json(pkg)


@router.get("/exports/{package_id}/download")
def download(package_id: int, session: Session = Depends(get_session)) -> FileResponse:
    p = _pkg(session, package_id)
    if p.direction != "export" or not p.path or not Path(p.path).is_file():
        raise HTTPException(404, "no package file")
    return FileResponse(p.path, media_type="application/zip", filename=Path(p.path).name)


# ---------------------------------------------------------------- import

class ImportIn(BaseModel):
    upload_id: str | None = None
    local_path: str | None = None


@router.post("/imports", status_code=201)
def stage_import(body: ImportIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    if bool(body.upload_id) == bool(body.local_path):
        raise HTTPException(400, "give exactly one of upload_id or local_path")
    if body.upload_id:
        src = _upload_dir(body.upload_id)
    else:
        p = Path(body.local_path or "").expanduser()
        src = p if p.is_file() and p.is_absolute() else _local_dir(body.local_path or "")
    pkg = _run(session, lambda: sync.stage(session, src))
    return {**sync.package_json(pkg), "manifest": pkg.manifest, "suggestions": sync.suggestions(session, pkg)}


@router.get("/imports/{package_id}")
def import_detail(package_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = _pkg(session, package_id)
    if p.direction != "import":
        raise HTTPException(404, "not an import")
    out = {**sync.package_json(p), "manifest": p.manifest, "conflict_list": p.conflicts, "rules": sync.RULES}
    if p.status == "staged":
        out["suggestions"] = sync.suggestions(session, p)
    return out


@router.get("/imports/{package_id}/plan")
def import_plan(package_id: int, target_dataset_id: int | None = None,
                session: Session = Depends(get_session)) -> dict[str, Any]:
    p = _pkg(session, package_id)
    if p.status != "staged":
        raise HTTPException(409, f"package is {p.status}")
    try:
        pl = sync.plan(session, p, target_dataset_id)
    except sync.SyncError as exc:
        raise HTTPException(422, str(exc)) from None
    pl.pop("_all_rows")
    return {**pl, "rules": sync.RULES}


class ApplyIn(BaseModel):
    target_dataset_id: int | None = None  # None: create a new dataset from the package
    new_name: str | None = None
    resolutions: dict[str, str] = Field(default_factory=dict)  # conflict key -> incoming | local


@router.post("/imports/{package_id}/apply")
def apply_import(package_id: int, body: ApplyIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = _pkg(session, package_id)
    pkg = _run(session, lambda: sync.apply(session, p, body.target_dataset_id, body.resolutions, body.new_name))
    return {**sync.package_json(pkg), "conflict_list": pkg.conflicts}


class ResolveIn(BaseModel):
    key: str
    choice: str  # incoming | local


@router.post("/imports/{package_id}/resolve")
def resolve_conflict(package_id: int, body: ResolveIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = _pkg(session, package_id)
    c = _run(session, lambda: sync.resolve(session, p, body.key, body.choice))
    return {"conflict": c, "package": sync.package_json(p)}


@router.post("/imports/{package_id}/undo")
def undo_import(package_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = _pkg(session, package_id)
    pkg = _run(session, lambda: sync.undo(session, p))
    return sync.package_json(pkg)
