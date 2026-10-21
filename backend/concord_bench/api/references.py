"""Reference packages in (CONTRACTS.md §2, §4; SPLIT_PLAN.md §4.3) and the endpoints Concord Label calls:
fingerprints, staged imports (accept / reject), and the latest scores of a source's newest version."""

from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_session
from ..core.models import Dataset, ReferenceImport, Run, RunTask, Score
from ..datasets import refpackage
from ..datasets.refpackage import PackageError
from .datasets import _clean_tags, _upload_dir, dataset_json, job_brief

router = APIRouter(tags=["reference packages"])


class FingerprintsIn(BaseModel):
    sha256: list[str] = Field(default_factory=list, max_length=200_000)


@router.post("/fingerprints/missing")
def fingerprints_missing(body: FingerprintsIn, session: Session = Depends(get_session)) -> dict[str, list[str]]:
    """The sha256s Bench has no original for, so a sender can leave the others out of a package."""
    return {"missing": refpackage.missing_originals(session, body.sha256)}


# ---------------------------------------------------------------- staged imports

class ImportIn(BaseModel):
    upload_id: str


@router.post("/reference-imports", status_code=201)
def stage_import(body: ImportIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    d = _upload_dir(body.upload_id)
    if session.scalar(select(ReferenceImport).where(ReferenceImport.upload_id == body.upload_id)) is not None:
        raise HTTPException(409, "this upload is already staged as a reference import")
    try:
        imp = refpackage.stage(session, body.upload_id, d)
    except PackageError as exc:
        session.rollback()
        shutil.rmtree(d, ignore_errors=True)  # a corrected package comes as a new upload
        raise HTTPException(400, str(exc)) from None
    session.commit()
    return {"id": imp.id, "status": imp.status, "preview": imp.preview}


def _get(session: Session, import_id: int) -> ReferenceImport:
    imp = session.get(ReferenceImport, import_id)
    if imp is None:
        raise HTTPException(404, "reference import not found")
    return imp


@router.get("/reference-imports")
def list_imports(status: str | None = None, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(ReferenceImport).order_by(ReferenceImport.id.desc())
    if status:
        stmt = stmt.where(ReferenceImport.status.in_(status.split(",")))
    return [refpackage.import_json(i) for i in session.scalars(stmt)]


@router.get("/reference-imports/{import_id}")
def get_import(import_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return refpackage.import_json(_get(session, import_id))


class PlanIn(BaseModel):
    seg_model_ids: list[int] = Field(default_factory=list)
    asr_model_ids: list[int] = Field(default_factory=list)
    oracle: bool = False
    mode: str = "grid"


class AcceptIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = None
    language: str | None = None
    plan: PlanIn | None = None


@router.post("/reference-imports/{import_id}/accept", status_code=202)
def accept_import(import_id: int, body: AcceptIn | None = None,
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    """A new dataset through the ingest job (never a change to an existing one)."""
    imp = _get(session, import_id)
    if imp.status != "staged":
        raise HTTPException(409, f"reference import is {imp.status}")
    body = body or AcceptIn()
    try:
        ds, job = refpackage.accept(session, imp, name=body.name,
                                    tags=_clean_tags(body.tags) if body.tags is not None else None,
                                    language=body.language, plan=body.plan.model_dump() if body.plan else None)
    except PackageError as exc:
        session.rollback()
        raise HTTPException(400, str(exc)) from None
    session.commit()
    return {"import": refpackage.import_json(imp), "dataset": dataset_json(session, ds), "job": job_brief(job)}


@router.post("/reference-imports/{import_id}/reject")
def reject_import(import_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    imp = _get(session, import_id)
    if imp.status != "staged":
        raise HTTPException(409, f"reference import is {imp.status}")
    refpackage.reject(session, imp)
    session.commit()
    return refpackage.import_json(imp)


# ---------------------------------------------------------------- scores for Label's live evaluation

def _iso(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


@router.get("/datasets/by-source/{dataset_uid}/scores")
def scores_by_source(dataset_uid: str, install_id: str | None = None, normaliser: str | None = None,
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    """Per model, its latest finished ASR run on the newest version of the source's dataset, scored with
    the requested normaliser when such a score exists, else its latest score (CONTRACTS.md §4)."""
    stmt = select(Dataset).where(Dataset.status == "ready", Dataset.source_key.is_not(None))
    cands = [d for d in session.scalars(stmt)
             if ((d.provenance or {}).get("source") or {}).get("dataset_uid") == dataset_uid
             and (install_id is None or ((d.provenance or {}).get("source") or {}).get("install_id") == install_id)]
    if not cands:
        raise HTTPException(404, "Concord Bench has no dataset from that source")
    ds = max(cands, key=lambda d: (d.export_version or 0, d.id))
    latest: dict[int, Run] = {}
    for run in session.scalars(select(Run).where(Run.dataset_id == ds.id, Run.task == RunTask.asr,
                                                 Run.status == "done").order_by(Run.id)):
        latest[run.model_id] = run
    scores = []
    for run in latest.values():
        all_scores = list(session.scalars(select(Score).where(Score.run_id == run.id).order_by(Score.id)))
        if not all_scores:
            continue
        wanted = [s for s in all_scores if normaliser and (s.normaliser == normaliser
                                                           or s.normaliser.split("@")[0] == normaliser)]
        sc = (wanted or all_scores)[-1]
        scores.append({"model": run.model.registry_name, "display_name": run.model.display_name,
                       "wer": (sc.aggregates or {}).get("wer"), "normaliser": sc.normaliser, "run_id": run.id,
                       "score_id": sc.id, "created_at": _iso(sc.created_at)})
    scores.sort(key=lambda s: (s["wer"] is None, s["wer"] if s["wer"] is not None else 0, s["model"]))
    return {"dataset": {"id": ds.id, "name": ds.name, "export_version": ds.export_version}, "scores": scores}
