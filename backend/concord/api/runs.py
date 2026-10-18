"""Runs, scores and jobs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_session
from ..core.models import Dataset, Job, JobStatus, Model, Pipeline, Run, RunTask, Score
from ..metrics import normalisers
from ..platform import services
from ..runs import jobs, scoring
from ..runs.executor import services_for
from .datasets import job_brief

router = APIRouter(tags=["runs"])
ACTIVE = ("queued", "running")


# ---------------------------------------------------------------- helpers shared with benchmarks

def get_or_create_pipeline(session: Session, seg_model_id: int | None, asr_model_id: int) -> Pipeline:
    p = session.scalar(select(Pipeline).where(Pipeline.asr_model_id == asr_model_id,
                                              Pipeline.seg_model_id.is_(None) if seg_model_id is None
                                              else Pipeline.seg_model_id == seg_model_id))
    if p is None:
        p = Pipeline(seg_model_id=seg_model_id, asr_model_id=asr_model_id)
        session.add(p)
        session.flush()
    return p


def run_label(run: Run) -> str:
    if run.task == RunTask.label:
        n = len(run.params.get("asr_model_ids") or [])
        return f"Pseudo-label · {n} ASR model{'s' if n != 1 else ''}"
    if run.task == RunTask.pipeline and run.pipeline:
        seg = run.pipeline.seg_model.display_name if run.pipeline.seg_model else "Reference segments + speakers"
        return f"{seg} → {run.pipeline.asr_model.display_name}"
    return run.model.display_name if run.model else f"run {run.id}"


def enqueue_run(session: Session, task: str, dataset: Dataset, model: Model | None = None,
                pipeline: Pipeline | None = None, normalisers_: list[str] | None = None,
                collar: float | None = None) -> tuple[Run, Job]:
    """Queue a run unless the same one is already queued or running (then return that)."""
    if dataset.status != "ready":
        raise HTTPException(409, f"dataset {dataset.name!r} is {dataset.status}")
    stmt = select(Run).where(Run.task == task, Run.dataset_id == dataset.id, Run.status.in_(ACTIVE))
    stmt = stmt.where(Run.pipeline_id == pipeline.id) if pipeline else stmt.where(Run.model_id == model.id)
    existing = session.scalar(stmt)
    if existing is not None:
        job = session.scalar(select(Job).where(Job.type == "run", Job.status.in_(ACTIVE))
                             .where(Job.payload["run_id"].as_integer() == existing.id))
        if job is not None:
            return existing, job
    run = Run(task=task, dataset_id=dataset.id, model_id=model.id if model else None,
              pipeline_id=pipeline.id if pipeline else None, status="queued")
    session.add(run)
    session.flush()
    session.refresh(run)
    job = jobs.enqueue(session, "run", {"run_id": run.id, "normalisers": normalisers_, "collar": collar},
                       services_for(run))
    return run, job


def run_json(run: Run, with_scores: list[Score] | None = None) -> dict[str, Any]:
    d = {"id": run.id, "task": run.task, "label": run_label(run), "model_id": run.model_id,
         "pipeline_id": run.pipeline_id, "dataset_id": run.dataset_id, "dataset": run.dataset.name,
         "status": run.status, "error": run.error, "image_tags": run.image_tags, "params": run.params,
         "rtf": run.rtf_by_stage, "created_at": run.created_at.isoformat(),
         "started_at": run.started_at.isoformat() if run.started_at else None,
         "finished_at": run.finished_at.isoformat() if run.finished_at else None}
    if run.pipeline:
        d["seg_model_id"] = run.pipeline.seg_model_id
        d["asr_model_id"] = run.pipeline.asr_model_id
    if with_scores is not None:
        d["scores"] = [{"id": s.id, "normaliser": s.normaliser, "collar": s.collar, "aggregates": s.aggregates,
                        "created_at": s.created_at.isoformat()} for s in with_scores]
    return d


def ensure_score(session: Session, run: Run, normaliser: str | None, collar: float | None,
                 queue: bool = True) -> tuple[Score | None, Job | None]:
    """The score for (run, normaliser, collar), computed now if it only needs local code; a platform
    normaliser whose service is down is queued as a score job instead."""
    if run.status != "done":
        return None, None
    sc = scoring.find_score(session, run, normaliser, collar)
    if sc is not None:
        return sc, None
    n = normalisers.get(normaliser or normalisers.DEFAULT) if run.task != RunTask.seg else None
    if n is not None and n.service and not services.health(n.service).up:
        if not queue:
            return None, None
        job = session.scalar(select(Job).where(Job.type == "score", Job.status.in_(ACTIVE))
                             .where(Job.payload["run_id"].as_integer() == run.id))
        if job is None:
            job = jobs.enqueue(session, "score", {"run_id": run.id, "normaliser": n.key, "collar": collar},
                               [n.service])
            session.commit()
        return None, job
    sc = scoring.score_run(session, run, normaliser, collar)
    session.commit()
    return sc, None


# ---------------------------------------------------------------- runs

class RunIn(BaseModel):
    task: str  # asr | seg | pipeline
    dataset_id: int
    model_id: int | None = None
    seg_model_id: int | None = None  # pipelines; None with oracle=True
    asr_model_id: int | None = None
    oracle: bool = False
    normaliser: str | None = None


@router.post("/runs", status_code=202)
def create_run(body: RunIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = session.get(Dataset, body.dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    norms = [normalisers.get(body.normaliser).key] if body.normaliser else None
    if body.task in (RunTask.asr, RunTask.seg):
        m = session.get(Model, body.model_id or -1)
        if m is None:
            raise HTTPException(404, "model not found")
        if body.task == RunTask.asr and not m.is_asr:
            raise HTTPException(422, f"{m.display_name} has no ASR capability")
        if body.task == RunTask.seg and not m.is_seg:
            raise HTTPException(422, f"{m.display_name} has no VAD / diarisation capability")
        run, job = enqueue_run(session, body.task, ds, model=m, normalisers_=norms)
    elif body.task == RunTask.pipeline:
        asr = session.get(Model, body.asr_model_id or -1)
        if asr is None or not asr.is_asr:
            raise HTTPException(422, "pipelines need an ASR model")
        seg_id = None
        if not body.oracle:
            seg = session.get(Model, body.seg_model_id or -1)
            if seg is None or not seg.is_seg:
                raise HTTPException(422, "pipelines need a VAD / diarisation model or oracle=true")
            seg_id = seg.id
        elif "diar" not in (ds.label_types or []):
            raise HTTPException(422, "the oracle stage needs speaker labels in the dataset")
        p = get_or_create_pipeline(session, seg_id, asr.id)
        run, job = enqueue_run(session, RunTask.pipeline, ds, pipeline=p, normalisers_=norms)
    else:
        raise HTTPException(422, f"unknown task {body.task!r}")
    session.commit()
    return {"run": run_json(run), "job": job_brief(job)}


@router.get("/runs")
def list_runs(dataset_id: int | None = None, model_id: int | None = None, task: str | None = None,
              status: str | None = None, limit: int = 200, session: Session = Depends(get_session)) -> list:
    stmt = select(Run).order_by(Run.id.desc()).limit(limit)
    if dataset_id:
        stmt = stmt.where(Run.dataset_id == dataset_id)
    if model_id:
        stmt = stmt.where(Run.model_id == model_id)
    if task:
        stmt = stmt.where(Run.task == task)
    if status:
        stmt = stmt.where(Run.status == status)
    return [run_json(r) for r in session.scalars(stmt)]


def get_run(session: Session, run_id: int) -> Run:
    r = session.get(Run, run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    return r


@router.get("/runs/{run_id}")
def run_detail(run_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    r = get_run(session, run_id)
    scores = list(session.scalars(select(Score).where(Score.run_id == r.id).order_by(Score.id)))
    return run_json(r, scores)


class ScoreIn(BaseModel):
    normaliser: str | None = None
    collar: float | None = None


@router.post("/runs/{run_id}/score")
def score(run_id: int, body: ScoreIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    r = get_run(session, run_id)
    if r.status != "done":
        raise HTTPException(409, f"run is {r.status}")
    try:
        sc, job = ensure_score(session, r, body.normaliser, body.collar)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from None
    if job is not None:
        return {"pending": True, "job": job_brief(job)}
    return {"pending": False, "score": {"id": sc.id, "normaliser": sc.normaliser, "collar": sc.collar,
                                        "aggregates": sc.aggregates}}


# ---------------------------------------------------------------- jobs

@router.get("/jobs")
def list_jobs(status: str | None = None, active: bool = False, limit: int = 50,
              session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(Job).order_by(Job.id.desc()).limit(limit)
    if active:
        stmt = stmt.where(Job.status.in_(ACTIVE))
    elif status:
        stmt = stmt.where(Job.status == status)
    out = []
    for j in session.scalars(stmt):
        d = job_brief(j)
        d["label"] = job_label(session, j)
        out.append(d)
    return out


def job_label(session: Session, j: Job) -> str:
    p = j.payload or {}
    if j.type == "run":
        r = session.get(Run, p.get("run_id"))
        return f"{run_label(r)} · {r.dataset.name}" if r else "run"
    if j.type == "ingest":
        d = session.get(Dataset, p.get("dataset_id"))
        return f"ingest · {d.name}" if d else "ingest"
    if j.type == "check":
        return f"check · {p.get('service')}"
    if j.type == "label":
        r = session.get(Run, p.get("run_id"))
        return f"pseudo-label · {r.dataset.name}" if r else "pseudo-label"
    if j.type == "score":
        return f"rescore · run {p.get('run_id')} · {p.get('normaliser')}"
    return j.type


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    j = session.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    d = job_brief(j)
    d["label"] = job_label(session, j)
    d["log"] = jobs.read_log(job_id)
    return d


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    j = session.get(Job, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    if j.status not in (JobStatus.failed, JobStatus.blocked):
        raise HTTPException(409, f"job is {j.status}")
    jobs.retry(session, j)
    session.commit()
    return job_brief(j)
