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
from ..platform import catalog
from ..runs import jobs, scoring
from ..runs.executor import services_for
from .datasets import job_brief

router = APIRouter(tags=["runs"])
ACTIVE = ("queued", "waiting", "running")


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


PLACEMENTS = ("before", "after")


def run_label(run: Run) -> str:
    if run.task == RunTask.pipeline and run.pipeline:
        seg = run.pipeline.seg_model.display_name if run.pipeline.seg_model else "Reference segments + speakers"
        base = f"{seg} → {run.pipeline.asr_model.display_name}"
    else:
        base = run.model.display_name if run.model else f"run {run.id}"
    if run.enh_model is not None:
        where = {"before": " before diarisation", "after": " after diarisation"}.get(run.enh_placement or "", "")
        return f"{run.enh_model.display_name}{where} ▸ {base}" if run.task == RunTask.pipeline else \
            f"{base} + {run.enh_model.display_name}"
    return base


def _active_run(session: Session, task: str, dataset: Dataset, model: Model | None,
                pipeline: Pipeline | None, enh_model: Model | None = None,
                placement: str | None = None) -> tuple[Run, Job] | None:
    stmt = select(Run).where(Run.task == task, Run.dataset_id == dataset.id, Run.status.in_(ACTIVE))
    stmt = stmt.where(Run.pipeline_id == pipeline.id) if pipeline else stmt.where(Run.model_id == model.id)
    stmt = stmt.where(Run.enh_model_id == enh_model.id) if enh_model else stmt.where(Run.enh_model_id.is_(None))
    stmt = stmt.where(Run.enh_placement == placement) if placement else stmt.where(Run.enh_placement.is_(None))
    existing = session.scalar(stmt)
    if existing is not None:
        job = session.scalar(select(Job).where(Job.type == "run", Job.status.in_(ACTIVE))
                             .where(Job.payload["run_id"].as_integer() == existing.id))
        if job is not None:
            return existing, job
    return None


def enqueue_run(session: Session, task: str, dataset: Dataset, model: Model | None = None,
                normalisers_: list[str] | None = None, collar: float | None = None,
                enh_model: Model | None = None) -> tuple[Run, Job]:
    """Queue a single-model run unless the same one is already queued or running (then return that).
    Its job leases the model; with an enhancement model, it reads that model's copy of the dataset and waits
    for the copy to be built. Pipelines go through `enqueue_pipelines`."""
    if dataset.status != "ready":
        raise HTTPException(409, f"dataset {dataset.name!r} is {dataset.status}")
    active = _active_run(session, task, dataset, model, None, enh_model)
    if active is not None:
        return active
    after = []
    if enh_model is not None:
        _, cj = jobs.copy_job(session, dataset, enh_model)
        after = [cj.id] if cj is not None else []
    run = Run(task=task, dataset_id=dataset.id, model_id=model.id, status="queued",
              enh_model_id=enh_model.id if enh_model else None)
    session.add(run)
    session.flush()
    session.refresh(run)
    job = jobs.enqueue(session, "run", {"run_id": run.id, "normalisers": normalisers_, "collar": collar},
                       services_for(run), after)
    return run, job


def enqueue_pipelines(session: Session, dataset: Dataset, pipelines: list[Pipeline],
                      normalisers_: list[str] | None = None, collar: float | None = None,
                      order: list[str] | None = None, enh_model: Model | None = None,
                      placement: str | None = None) -> list[tuple[Run, Job]]:
    """Pipelines stage by stage (MODEL_CATALOG_REDESIGN.md, "Job planning"): one seg-stage job per
    segmenter over every file, one ASR-stage job per ASR model over the cuts of every seg source (oracle
    included), then one assemble-and-score run job per pipeline that needs no model. Each stage job leases
    only its own model, so each model loads once. `order` (catalog model ids, from the catalog's plan)
    decides which stage job of a stage comes first. Pipelines already queued or running are returned as they are.

    With an enhancement model, the pipelines read its copy of the dataset (EVALUATE_PLAN.md §5.4): `before`
    diarisation, both stages hear the copy; `after`, diarisation hears the original and ASR the copy. The
    oracle has no diarisation, so its placement is None. Every job waits for the copy to be built."""
    if dataset.status != "ready":
        raise HTTPException(409, f"dataset {dataset.name!r} is {dataset.status}")
    if enh_model is not None and placement not in PLACEMENTS:
        raise HTTPException(422, f"placement must be one of {', '.join(PLACEMENTS)}")
    copy_after: list[int] = []
    if enh_model is not None:
        _, cj = jobs.copy_job(session, dataset, enh_model)
        copy_after = [cj.id] if cj is not None else []
    enh_id = enh_model.id if enh_model else None
    out: list[tuple[Run, Job]] = []
    todo: list[Pipeline] = []
    for p in pipelines:
        where = placement if enh_model is not None and p.seg_model_id is not None else None
        active = _active_run(session, RunTask.pipeline, dataset, None, p, enh_model, where)
        if active is not None:
            out.append(active)
        else:
            todo.append(p)
    rank = {cid: i for i, cid in enumerate(order or [])}

    def by_plan(models: list[Model]) -> list[Model]:
        return sorted(dict.fromkeys(models), key=lambda m: rank.get(m.catalog_id, len(rank)))

    seg_enh = enh_id if placement == "before" else None  # the audio diarisation hears
    seg_jobs = {m.id: jobs.enqueue_stage(session, "seg_stage", dataset.id, m, after=copy_after if seg_enh else None,
                                         enh_model_id=seg_enh)
                for m in by_plan([p.seg_model for p in todo if p.seg_model is not None])}
    asr_jobs = {}
    for a in by_plan([p.asr_model for p in todo]):
        segs = [p.seg_model_id for p in todo if p.asr_model_id == a.id]
        sources = [{"seg": s, "seg_enh": seg_enh if s is not None else None} for s in segs]
        asr_jobs[a.id] = jobs.enqueue_stage(session, "asr_stage", dataset.id, a, sources,
                                            after=[seg_jobs[s].id for s in segs if s is not None] + copy_after,
                                            enh_model_id=enh_id)
    for p in todo:
        run = Run(task=RunTask.pipeline, dataset_id=dataset.id, pipeline_id=p.id, status="queued", enh_model_id=enh_id,
                  enh_placement=placement if enh_model is not None and p.seg_model_id is not None else None)
        session.add(run)
        session.flush()
        after = [asr_jobs[p.asr_model_id].id] + ([seg_jobs[p.seg_model_id].id] if p.seg_model_id else []) + copy_after
        job = jobs.enqueue(session, "run", {"run_id": run.id, "normalisers": normalisers_, "collar": collar,
                                            "assemble": True}, [], after)
        out.append((run, job))
    return out


def run_json(run: Run, with_scores: list[Score] | None = None) -> dict[str, Any]:
    d = {"id": run.id, "task": run.task, "label": run_label(run), "model_id": run.model_id,
         "enh_model_id": run.enh_model_id, "enh_copy_id": run.enh_copy_id, "enh_placement": run.enh_placement,
         "enhancer": run.enh_model.display_name if run.enh_model else None,
         "generative": bool(run.enh_model and run.enh_model.generative),
         "pipeline_id": run.pipeline_id, "dataset_id": run.dataset_id, "dataset": run.dataset.name,
         "status": run.status, "error": run.error, "builds": run.builds, "params": run.params,
         "rtf": run.rtf_by_stage, "hardware": run.hardware or {}, "wall_s": run.wall_s,
         "created_at": run.created_at.isoformat(),
         "started_at": run.started_at.isoformat() if run.started_at else None,
         "finished_at": run.finished_at.isoformat() if run.finished_at else None}
    if run.pipeline:
        d["seg_model_id"] = run.pipeline.seg_model_id
        d["asr_model_id"] = run.pipeline.asr_model_id
    if with_scores is not None:
        d["scores"] = [{"id": s.id, "normaliser": s.normaliser, "collar": s.collar,
                        "skip_overlap": s.skip_overlap, "aggregates": s.aggregates,
                        "created_at": s.created_at.isoformat()} for s in with_scores]
    return d


def ensure_score(session: Session, run: Run, normaliser: str | None, collar: float | None,
                 queue: bool = True, skip_overlap: bool = False) -> tuple[Score | None, Job | None]:
    """The score for (run, normaliser, collar, skip_overlap), computed now if it only needs local code. A
    platform normaliser whose model is not running in the catalog is queued as a score job instead: the call
    goes through the catalog's gateway, which starts the model, and that can take minutes."""
    if run.status != "done":
        return None, None
    sc = scoring.find_score(session, run, normaliser, collar, skip_overlap)
    if sc is not None:
        return sc, None
    n = normalisers.get(normaliser or normalisers.DEFAULT) if run.task != RunTask.seg else None
    if n is not None and n.service and _model_state(n.service) != "running":
        if not queue:
            return None, None
        job = session.scalar(select(Job).where(Job.type == "score", Job.status.in_(ACTIVE))
                             .where(Job.payload["run_id"].as_integer() == run.id))
        if job is None:
            job = jobs.enqueue(session, "score", {"run_id": run.id, "normaliser": n.key, "collar": collar,
                                                  "skip_overlap": skip_overlap})
            session.commit()
        return None, job
    sc = scoring.score_run(session, run, normaliser, collar, skip_overlap)
    session.commit()
    return sc, None


def _model_state(model_id: str) -> str | None:
    try:
        rec = catalog.record(model_id)
    except catalog.CatalogUnavailable:
        return None
    return rec.get("state") if rec else None


# ---------------------------------------------------------------- runs

def resolve_model(session: Session, ref: int | str | None) -> Model:
    """A model by Bench model id (int) or catalog model id (str); a catalog model gets its row on first use."""
    if isinstance(ref, str):
        try:
            return catalog.ensure_model(session, ref)
        except catalog.CatalogUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except catalog.ModelUnusable as exc:
            raise HTTPException(404, str(exc)) from None
    m = session.get(Model, ref) if ref is not None else None
    if m is None:
        raise HTTPException(404, "model not found")
    return m


class RunIn(BaseModel):
    task: str  # asr | seg | pipeline
    dataset_id: int
    model_id: int | str | None = None  # Bench model id, or catalog model id
    seg_model_id: int | str | None = None  # pipelines; None with oracle=True
    asr_model_id: int | str | None = None
    oracle: bool = False
    normaliser: str | None = None
    enh_model_id: int | str | None = None  # read this enhancement model's copy of the dataset
    placement: str | None = None  # pipelines with an enhancer: before | after diarisation


@router.post("/runs", status_code=202)
def create_run(body: RunIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = session.get(Dataset, body.dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    norms = [normalisers.get(body.normaliser).key] if body.normaliser else None
    enh = resolve_model(session, body.enh_model_id) if body.enh_model_id is not None else None
    if enh is not None and not enh.is_enh:
        raise HTTPException(422, f"{enh.display_name} is not a speech enhancement model")
    if body.task in (RunTask.asr, RunTask.seg):
        m = resolve_model(session, body.model_id)
        if body.task == RunTask.asr and not m.is_asr:
            raise HTTPException(422, f"{m.display_name} has no ASR capability")
        if body.task == RunTask.seg and not m.is_seg:
            raise HTTPException(422, f"{m.display_name} has no VAD / diarisation capability")
        run, job = enqueue_run(session, body.task, ds, model=m, normalisers_=norms, enh_model=enh)
    elif body.task == RunTask.pipeline:
        asr = resolve_model(session, body.asr_model_id)
        if not asr.is_asr:
            raise HTTPException(422, "pipelines need an ASR model")
        seg_id = None
        if not body.oracle:
            seg = resolve_model(session, body.seg_model_id)
            if not seg.is_seg:
                raise HTTPException(422, "pipelines need a VAD / diarisation model or oracle=true")
            seg_id = seg.id
        elif "diar" not in (ds.label_types or []):
            raise HTTPException(422, "the oracle stage needs speaker labels in the dataset")
        p = get_or_create_pipeline(session, seg_id, asr.id)
        run, job = enqueue_pipelines(session, ds, [p], normalisers_=norms, enh_model=enh,
                                     placement=(body.placement or "before") if enh else None)[0]
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
    skip_overlap: bool = False


@router.post("/runs/{run_id}/score")
def score(run_id: int, body: ScoreIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    r = get_run(session, run_id)
    if r.status != "done":
        raise HTTPException(409, f"run is {r.status}")
    try:
        sc, job = ensure_score(session, r, body.normaliser, body.collar, skip_overlap=body.skip_overlap)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from None
    if job is not None:
        return {"pending": True, "job": job_brief(job)}
    return {"pending": False, "score": {"id": sc.id, "normaliser": sc.normaliser, "collar": sc.collar,
                                        "skip_overlap": sc.skip_overlap, "aggregates": sc.aggregates}}


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
    if j.type in ("seg_stage", "asr_stage"):
        m, d = session.get(Model, p.get("model_id")), session.get(Dataset, p.get("dataset_id"))
        stage = "seg stage" if j.type == "seg_stage" else "ASR stage"
        e = session.get(Model, p["enh_model_id"]) if p.get("enh_model_id") else None
        return f"{stage} · {m.display_name if m else 'model'} · {d.name if d else 'dataset'}" + \
            (f" · {e.display_name} copy" if e else "")
    if j.type == "ingest":
        d = session.get(Dataset, p.get("dataset_id"))
        return f"ingest · {d.name}" if d else "ingest"
    if j.type == "check":
        return f"check · {p.get('service')}"
    if j.type == "score":
        return f"rescore · run {p.get('run_id')} · {p.get('normaliser')}"
    if j.type == "enhance":
        m, d = session.get(Model, p.get("model_id")), session.get(Dataset, p.get("dataset_id"))
        return f"enhance · {m.display_name if m else 'model'} · {d.name if d else 'dataset'}"
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
