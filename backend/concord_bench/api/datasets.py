"""Datasets: staged uploads (files or chunked zips), local-path ingest, inspection, templates."""

from __future__ import annotations

import secrets
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import peer
from ..core import paths
from ..core.db import get_session
from ..core.models import AudioFile, Dataset, EnhancedCopy, Job, JobStatus, Model, Run, RunTask, Segment, Slice
from ..core.settings import REPO_ROOT
from ..datasets.ingest import NO_REFERENCES, inspect_source
from ..platform import catalog, dataset_catalog
from ..runs import enhance, jobs

router = APIRouter(tags=["datasets"])
TEMPLATES = REPO_ROOT / "fixtures" / "generated" / "templates"


def _safe_rel(rel: str) -> str:
    """A relative path inside the upload folder: leading slashes dropped, `..` refused."""
    parts = [x for x in PurePosixPath(rel.replace("\\", "/")).parts if x not in ("", ".", "/")]
    if not parts or ".." in parts:
        raise HTTPException(400, f"unsafe path {rel!r}")
    return "/".join(parts)


def _upload_dir(upload_id: str) -> Path:
    if not upload_id.isalnum():
        raise HTTPException(400, "bad upload id")
    d = paths.uploads(upload_id)
    if not d.is_dir():
        raise HTTPException(404, "upload not found")
    return d


# ---------------------------------------------------------------- uploads

@router.post("/uploads")
def create_upload() -> dict[str, str]:
    uid = secrets.token_hex(8)
    paths.uploads(uid).mkdir(parents=True)
    return {"upload_id": uid}


@router.post("/uploads/{upload_id}/files")
async def upload_files(upload_id: str, files: list[UploadFile] = File(...),
                       paths_: list[str] | None = Form(default=None, alias="paths")) -> dict[str, Any]:
    """Several files per request; `paths` (same order) keeps folder structure from a folder picker."""
    d = _upload_dir(upload_id)
    received = 0
    for k, f in enumerate(files):
        rel = _safe_rel(paths_[k] if paths_ and k < len(paths_) else (f.filename or f"file{k}"))
        dest = d / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
                received += len(chunk)
    return {"files": len(files), "bytes": received}


@router.put("/uploads/{upload_id}/chunk")
async def upload_chunk(upload_id: str, request: Request, path: str = Query(...), offset: int = Query(0)) -> dict:
    """Chunked upload of one large file (e.g. a zip): send consecutive byte ranges with their offset."""
    d = _upload_dir(upload_id)
    dest = d / _safe_rel(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = dest.stat().st_size if dest.exists() else 0
    if offset != size:
        raise HTTPException(409, f"expected offset {size}, got {offset}")
    with open(dest, "ab") as out:
        async for chunk in request.stream():
            out.write(chunk)
    return {"path": dest.relative_to(d).as_posix(), "size": dest.stat().st_size}


@router.get("/uploads/{upload_id}/inspect")
def inspect_upload(upload_id: str) -> dict[str, Any]:
    try:
        return inspect_source(_upload_dir(upload_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


class PathIn(BaseModel):
    path: str


def _local_dir(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() or not p.is_dir():
        raise HTTPException(400, f"{path!r} is not an absolute path to a folder on this machine")
    return p


@router.post("/uploads/inspect-path")
def inspect_path(body: PathIn) -> dict[str, Any]:
    try:
        return inspect_source(_local_dir(body.path))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


# ---------------------------------------------------------------- datasets

class PlanIn(BaseModel):
    """A benchmark plan queued once the upload is ingested (the fields of api.benchmarks.PlanIn)."""
    seg_models: list[str] = Field(default_factory=list)  # catalog model ids
    asr_models: list[str] = Field(default_factory=list)
    seg_model_ids: list[int] = Field(default_factory=list)  # Bench model ids
    asr_model_ids: list[int] = Field(default_factory=list)
    oracle: bool = False
    mode: str = "grid"  # grid | stages
    enhancers: list[str] = Field(default_factory=list)  # enhancement models: each builds a copy at ingest
    include_original: bool = True
    placements: list[str] = Field(default_factory=lambda: ["before"])


class DatasetIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    language: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    upload_id: str | None = None
    local_path: str | None = None
    plan: PlanIn | None = None


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = t.strip().lstrip("#")
        if t and t not in out:
            out.append(t[:60])
    return out


def dataset_json(session: Session, ds: Dataset, detail: bool = False) -> dict[str, Any]:
    rep = ds.report or {}
    d: dict[str, Any] = {
        "id": ds.id, "name": ds.name, "language": ds.language, "description": ds.description, "tags": ds.tags,
        "label_types": ds.label_types, "status": ds.status, "created_at": ds.created_at.isoformat(),
        "files": rep.get("files"), "usable_files": rep.get("usable_files"), "hours": rep.get("hours"),
        "usable_hours": rep.get("usable_hours"), "has_speaker_text": bool(rep.get("has_speaker_text")),
        "flagged": {k: len(v) for k, v in (rep.get("flagged") or {}).items()},
        "provenance": ds.provenance or {}, "export_version": ds.export_version,
        "previous_version_id": ds.previous_version_id,
    }
    if detail:
        d["versions"] = [{"id": v.id, "name": v.name, "export_version": v.export_version, "status": v.status,
                          "created_at": v.created_at.isoformat()}
                         for v in session.scalars(select(Dataset).where(Dataset.source_key == ds.source_key)
                                                  .order_by(Dataset.export_version, Dataset.id))] \
            if ds.source_key else []
        d["previous_plan"] = previous_plan(session, ds)
        d["report"] = rep
        d["runs"] = session.scalar(select(func.count(Run.id)).where(Run.dataset_id == ds.id))
        job = session.scalar(select(Job).where(Job.type == "ingest").order_by(Job.id.desc())
                             .where(Job.payload["dataset_id"].as_integer() == ds.id))
        d["ingest_job"] = job_brief(job) if job else None
        d["enhanced"] = copies_of(session, ds)
        d["slices"] = [slice_json(s) for s in session.scalars(select(Slice).where(Slice.dataset_id == ds.id)
                                                               .order_by(Slice.id))]
    else:
        d["enhanced"] = [{"enh_model_id": c["model_id"], "display_name": c["display_name"], "status": c["status"],
                          "progress": c["progress"]} for c in copies_of(session, ds)]
    return d


def copies_of(session: Session, ds: Dataset) -> list[dict[str, Any]]:
    """The dataset's enhanced copies, newest per enhancement model (deleted ones left out), with run counts."""
    newest: dict[int, EnhancedCopy] = {}
    for c in session.scalars(select(EnhancedCopy).where(EnhancedCopy.dataset_id == ds.id,
                                                        EnhancedCopy.status != "deleted").order_by(EnhancedCopy.id)):
        newest[c.model_id] = c
    runs = dict(session.execute(select(Run.enh_copy_id, func.count(Run.id)).where(Run.dataset_id == ds.id)
                                .group_by(Run.enh_copy_id)).all())
    return [enhance.copy_json(c, runs.get(c.id, 0)) for c in sorted(newest.values(), key=lambda c: c.model.display_name)]


def previous_plan(session: Session, ds: Dataset) -> dict[str, Any] | None:
    """"Run the models benchmarked on the previous version against this one" (SPLIT_PLAN.md §4.3): a
    benchmark plan pre-ticked with the models that have done runs on the previous version."""
    prev = session.get(Dataset, ds.previous_version_id) if ds.previous_version_id else None
    if prev is None:
        return None
    seg: set[int] = set()
    asr: set[int] = set()
    oracle = pipelines = False
    for r in session.scalars(select(Run).where(Run.dataset_id == prev.id, Run.status == "done")):
        if r.task == RunTask.asr and r.model_id:
            asr.add(r.model_id)
        elif r.task == RunTask.seg and r.model_id:
            seg.add(r.model_id)
        elif r.task == RunTask.pipeline and r.pipeline is not None:
            pipelines = True
            asr.add(r.pipeline.asr_model_id)
            if r.pipeline.seg_model_id is None:
                oracle = True
            else:
                seg.add(r.pipeline.seg_model_id)
    already = {r.model_id for r in session.scalars(select(Run).where(Run.dataset_id == ds.id)) if r.model_id}

    def cids(ids: set[int]) -> list[str]:
        return sorted(m.catalog_id for m in (session.get(Model, i) for i in ids) if m is not None)

    return {"previous": {"id": prev.id, "name": prev.name, "export_version": prev.export_version},
            "plan": {"seg_models": cids(seg), "asr_models": cids(asr), "oracle": oracle,
                     "mode": "grid" if pipelines else "stages"},
            "models": len(seg | asr), "already_run": bool(already & (seg | asr))}


def job_brief(job: Job) -> dict[str, Any]:
    live = jobs.live_progress(job.id) if job.status == "running" else None
    return {"id": job.id, "type": job.type, "status": job.status,
            "progress": live["progress"] if live else job.progress,
            "message": live["message"] if live else job.message,
            "error": job.error, "required_services": job.required_services, "payload": job.payload,
            "after": job.after or [], "lease": job.lease,
            "result": job.result, "created_at": job.created_at.isoformat(),
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None}


@router.post("/datasets", status_code=201)
def create_dataset(body: DatasetIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    if bool(body.upload_id) == bool(body.local_path):
        raise HTTPException(400, "give exactly one of upload_id or local_path")
    source = _upload_dir(body.upload_id) if body.upload_id else _local_dir(body.local_path or "")
    try:
        found = inspect_source(source)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if found["audio_files"] and len(found["audio_without_labels"]) == found["audio_files"]:
        raise HTTPException(400, NO_REFERENCES)
    ds = Dataset(name=body.name.strip(), language=(body.language or "").strip() or None,
                 description=body.description, tags=_clean_tags(body.tags), status="ingesting")
    session.add(ds)
    session.flush()
    job = jobs.enqueue(session, "ingest", {"dataset_id": ds.id, "source": str(source),
                                           "cleanup": bool(body.upload_id),
                                           "plan": body.plan.model_dump() if body.plan else None})
    session.commit()
    return {"dataset": dataset_json(session, ds), "job": job_brief(job)}


@router.get("/datasets")
def list_datasets(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [dataset_json(session, d) for d in session.scalars(select(Dataset).order_by(Dataset.created_at.desc()))]


def get_dataset(session: Session, dataset_id: int) -> Dataset:
    ds = session.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    return ds


@router.get("/datasets/{dataset_id}")
def dataset_detail(dataset_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return dataset_json(session, get_dataset(session, dataset_id), detail=True)


class DatasetPatch(BaseModel):
    name: str | None = None
    language: str | None = None
    description: str | None = None
    tags: list[str] | None = None


@router.patch("/datasets/{dataset_id}")
def patch_dataset(dataset_id: int, body: DatasetPatch, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = get_dataset(session, dataset_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(ds, k, _clean_tags(v) if k == "tags" else v)
    session.commit()
    return dataset_json(session, ds)


def slice_json(s: Slice) -> dict[str, Any]:
    return {"id": s.id, "dataset_id": s.dataset_id, "name": s.name, "filters": s.filters,
            "created_at": s.created_at.isoformat()}


class SliceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    filters: list[dict[str, Any]] = Field(min_length=1)


@router.get("/datasets/{dataset_id}/slices")
def list_slices(dataset_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    get_dataset(session, dataset_id)
    return [slice_json(s) for s in session.scalars(select(Slice).where(Slice.dataset_id == dataset_id).order_by(Slice.id))]


@router.post("/datasets/{dataset_id}/slices", status_code=201)
def save_slice(dataset_id: int, body: SliceIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """A saved slice belongs to this one dataset; Benchmarks scores it for every model run on it."""
    get_dataset(session, dataset_id)
    for f in body.filters:
        if "key" not in f or not ("value" in f or "min" in f or "max" in f):
            raise HTTPException(422, "each filter needs a key and a value, or a key with min / max")
    s = Slice(dataset_id=dataset_id, name=body.name.strip(), filters=body.filters)
    session.add(s)
    session.commit()
    return slice_json(s)


@router.delete("/slices/{slice_id}", status_code=204)
def delete_slice(slice_id: int, session: Session = Depends(get_session)) -> None:
    s = session.get(Slice, slice_id)
    if s is None:
        raise HTTPException(404, "slice not found")
    session.delete(s)
    session.commit()


class EnhanceIn(BaseModel):
    models: list[str] = Field(min_length=1)  # catalog ids of enhancement models


@router.get("/datasets/{dataset_id}/enhanced")
def dataset_enhanced(dataset_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return copies_of(session, get_dataset(session, dataset_id))


@router.post("/datasets/{dataset_id}/enhanced", status_code=202)
def build_enhanced(dataset_id: int, body: EnhanceIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Build each enhancement model's copy of the dataset as a job (one already built or building is kept)."""
    ds = get_dataset(session, dataset_id)
    if ds.status != "ready":
        raise HTTPException(409, f"dataset is {ds.status}")
    out = []
    for cid in dict.fromkeys(body.models):
        try:
            m = catalog.ensure_model(session, cid)
        except catalog.ModelUnusable as exc:
            raise HTTPException(404, str(exc)) from None
        except catalog.CatalogUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        if not m.is_enh:
            raise HTTPException(422, f"{m.display_name} is not a speech enhancement model")
        c, job = jobs.copy_job(session, ds, m)
        out.append({"copy": enhance.copy_json(c), "job": job_brief(job) if job else None})
    session.commit()
    return {"copies": out}


@router.delete("/enhanced/{copy_id}")
def delete_enhanced(copy_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Delete a ready (or failed) copy's audio, keeping the scores of runs that read it; or cancel a build."""
    c = session.get(EnhancedCopy, copy_id)
    if c is None:
        raise HTTPException(404, "enhanced copy not found")
    job = session.get(Job, c.job_id) if c.job_id else None
    if c.status == "building":
        c.status = "cancelled"
        if job is not None and job.status in (JobStatus.queued, JobStatus.waiting):
            job.status, job.error = JobStatus.failed, "cancelled before it started"
        session.commit()  # a running build stops at its next file and removes what it wrote
        return {"copy": enhance.copy_json(c), "freed": 0}
    if c.status == "deleted":
        return {"copy": enhance.copy_json(c), "freed": 0}
    freed = enhance.delete(session, c)
    session.commit()
    return {"copy": enhance.copy_json(c), "freed": freed}


@router.delete("/datasets/{dataset_id}", status_code=204)
def delete_dataset(dataset_id: int, session: Session = Depends(get_session)) -> None:
    """With the Dataset Catalog set up, the holds on the dataset's audio are released first, except on files
    another Bench dataset has; a catalog that doesn't answer keeps the dataset (and its holds)."""
    ds = get_dataset(session, dataset_id)
    if dataset_catalog.enabled():
        try:
            dataset_catalog.release_unused(
                session, session.scalars(select(AudioFile.sha256).where(AudioFile.dataset_id == ds.id)), ds.id)
        except (dataset_catalog.Unavailable, dataset_catalog.DatasetCatalogError) as exc:
            raise HTTPException(503, f"{exc}. The dataset is kept, so its audio stays held in the store; try "
                                     "again when the catalog answers.") from None
    session.delete(ds)
    session.commit()


@router.get("/datasets/{dataset_id}/files")
def dataset_files(dataset_id: int, status: str | None = None, q: str | None = None,
                  session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    get_dataset(session, dataset_id)
    stmt = select(AudioFile).where(AudioFile.dataset_id == dataset_id).order_by(AudioFile.rel_path)
    if status:
        stmt = stmt.where(AudioFile.status == status)
    if q:
        stmt = stmt.where(AudioFile.rel_path.contains(q))
    counts = dict(session.execute(select(Segment.audio_file_id, func.count(Segment.id))
                                  .join(AudioFile).where(AudioFile.dataset_id == dataset_id)
                                  .group_by(Segment.audio_file_id)).all())
    return [{"id": f.id, "path": f.rel_path, "stem": f.stem, "sha256": f.sha256, "duration": f.duration,
             "sample_rate": f.orig_sr, "channels": f.channels, "status": f.status, "segments": counts.get(f.id, 0),
             "meta": f.meta} for f in session.scalars(stmt)]


# ---------------------------------------------------------------- segments

class FlagIn(BaseModel):
    flagged: bool = True
    note: str | None = None


@router.get("/datasets/{dataset_id}/flags")
def dataset_flags(dataset_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """References flagged as wrong from the error pages ("Flag reference as wrong")."""
    get_dataset(session, dataset_id)
    stmt = (select(Segment, AudioFile).join(AudioFile)
            .where(AudioFile.dataset_id == dataset_id, Segment.flagged.is_(True))
            .order_by(AudioFile.rel_path, Segment.start))
    return [{"id": seg.id, "uid": seg.uid, "file_id": f.id, "path": f.rel_path, "sha256": f.sha256,
             "start": seg.start, "end": seg.end, "speaker": seg.speaker, "text": seg.text, "note": seg.flag_note,
             "updated_at": seg.updated_at.isoformat() if seg.updated_at else None}
            for seg, f in session.execute(stmt).all()]


@router.get("/datasets/{dataset_id}/flags.json")
def dataset_flags_file(dataset_id: int, session: Session = Depends(get_session)) -> JSONResponse:
    """The flags file for Concord Label (CONTRACTS.md §3), as a download."""
    ds = get_dataset(session, dataset_id)
    try:
        doc = peer.flags_file(session, ds)
    except peer.PeerError as exc:
        raise HTTPException(409, str(exc)) from None
    stem = "".join(c if c.isalnum() or c in "-_" else "-" for c in ds.name).strip("-") or f"dataset-{ds.id}"
    return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="{stem}-flags.json"'})


@router.post("/datasets/{dataset_id}/flags/send")
def send_flags(dataset_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Push the flags file to `LABEL_URL/api/flags/import`: {"matched", "unmatched", "sent"}."""
    ds = get_dataset(session, dataset_id)
    try:
        doc = peer.flags_file(session, ds)
        if not doc["flags"]:
            raise HTTPException(409, "no references are flagged")
        return {**peer.push_flags(doc), "sent": len(doc["flags"])}
    except peer.PeerError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/segments/{segment_id}/flag")
def flag_segment(segment_id: int, body: FlagIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    seg = session.get(Segment, segment_id)
    if seg is None:
        raise HTTPException(404, "segment not found")
    seg.flagged, seg.flag_note = body.flagged, body.note
    session.commit()
    return {"id": seg.id, "flagged": seg.flagged, "note": seg.flag_note}


# ---------------------------------------------------------------- templates

@router.get("/templates")
def list_templates() -> list[dict[str, Any]]:
    if not TEMPLATES.is_dir():
        return []
    return [{"name": p.name, "size": p.stat().st_size} for p in sorted(TEMPLATES.iterdir()) if p.is_file()]


@router.get("/templates/{name}")
def get_template(name: str) -> FileResponse:
    p = (TEMPLATES / name).resolve()
    if not p.is_file() or p.parent != TEMPLATES.resolve():
        raise HTTPException(404, "template not found")
    return FileResponse(p, filename=name)
