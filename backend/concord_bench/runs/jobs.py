"""The job queue: one worker process reading the `job` table (no Redis / Celery).

Job types:
- ingest     {dataset_id, source, cleanup, plan?, reference_import_id?}
                                          copy, decode, derive, parse references; for a reference package,
                                          then its uids and seg-model outputs
- run        {run_id, normalisers?, collar?, assemble?}
                                          a single run's inference, or with `assemble` a pipeline put together
                                          from its stages' stored outputs (no model); then scoring
- seg_stage  {dataset_id, model_id, enh_model_id?}
                                          one segmenter over every usable file of a dataset (a grid's seg stage),
                                          on the original audio or an enhancement model's copy
- asr_stage  {dataset_id, model_id, sources, enh_model_id?}
                                          one ASR model over the cuts of every seg source of a grid: {seg: Bench
                                          model id or None for the oracle, seg_enh: the copy the segmenter heard}
- enhance    {dataset_id, model_id, copy_id}
                                          an enhanced copy of a dataset (runs.enhance); needs the enhancement model
- score      {run_id, normaliser, collar, skip_overlap?}
                                          rescoring with a platform normaliser (through the catalog's gateway)

Models come from the Model Catalog (platform/catalog/README.md). A job that needs models
(`required_services`, catalog model ids) takes one catalog lease on them as `bench:job-<id>`; the aicore
client heartbeats it until the job ends, so neither the catalog nor Concord Label's jobs stop them
mid-run. A lease the catalog cannot grant yet waits in its queue: the job is `waiting` (queue position and
what it waits for in `job.lease`) and the worker runs jobs that need no model meanwhile, with at most
one lease waiting at a time. Stage jobs look their outputs up first (locally and in the catalog's output
index) and lease nothing when nothing is missing. Among runnable jobs, those whose models are already
running go first. `after` lists jobs that must be done first (a grid's stages): when one of them fails,
the job is `blocked`, and retrying it requeues them. A model the catalog does not know or has not
verified, or a lease that fails, blocks the job with the reason.
"""

from __future__ import annotations

import json
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from aicore.catalog import Lease
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core import paths
from ..core.db import SessionLocal
from ..core.models import Dataset, EnhancedCopy, Job, JobStatus, Model, Run, utcnow
from ..platform import catalog
from . import enhance, executor, scoring

LEASE_DONE = ("failed", "expired", "released")
_waiting: dict[int, Lease] = {}  # job id -> its lease, queued in the catalog (this worker process)


class JobBlocked(Exception):
    """The job cannot run: unknown or unverified model, a failed lease, a failed earlier stage."""


class JobContext:
    """Log lines and progress go to files beside the job, never to the database: the job's own
    session may hold SQLite's single write lock, and a second writer would wait on it."""

    def __init__(self, job_id: int):
        self.job_id = job_id
        self.log_path = paths.job_log(job_id)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._progress = 0.0
        self._message = ""

    def _write_progress(self) -> None:
        p = paths.job_progress(self.job_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"progress": self._progress, "message": self._message[:300]}))
        tmp.replace(p)

    def log(self, msg: str) -> None:
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%H:%M:%S} {msg}\n")
        self._message = msg
        self._write_progress()

    def progress(self, p: float, msg: str | None = None) -> None:
        self._progress = max(0.0, min(1.0, p))
        if msg:
            self._message = msg
        self._write_progress()

    @property
    def message(self) -> str:
        return self._message


def live_progress(job_id: int) -> dict[str, Any] | None:
    """Progress of a running job, from its progress file."""
    try:
        return json.loads(paths.job_progress(job_id).read_text())
    except (OSError, ValueError):
        return None


def enqueue(session: Session, type_: str, payload: dict[str, Any], required_services: list[str] | None = None,
            after: list[int] | None = None) -> Job:
    job = Job(type=type_, payload=payload, required_services=sorted(set(required_services or [])),
              after=sorted(set(after or [])))
    session.add(job)
    session.flush()
    return job


def source_spec(src: Any) -> dict[str, Any]:
    """An ASR stage source as stored: {seg, seg_enh}. Jobs queued before enhancement hold bare model ids."""
    return src if isinstance(src, dict) else {"seg": src, "seg_enh": None}


def _merge_sources(*lists: list[Any]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for src in (s for lst in lists for s in lst or []):
        spec = source_spec(src)
        out.setdefault(json.dumps(spec, sort_keys=True), spec)
    return list(out.values())


def enqueue_stage(session: Session, type_: str, dataset_id: int, model: Model, sources: list[Any] | None = None,
                  after: list[int] | None = None, enh_model_id: int | None = None) -> Job:
    """A grid stage job for one model on one dataset and audio version (original, or an enhancement model's
    copy); merged into a queued one for the same model, dataset and audio."""
    for j in session.scalars(select(Job).where(Job.type == type_, Job.status == JobStatus.queued)):
        p = j.payload or {}
        if p.get("dataset_id") == dataset_id and p.get("model_id") == model.id and p.get("enh_model_id") == enh_model_id:
            if sources is not None:
                j.payload = {**p, "sources": _merge_sources(p.get("sources"), sources)}
            j.after = sorted(set(j.after or []) | set(after or []))
            return j
    payload: dict[str, Any] = {"dataset_id": dataset_id, "model_id": model.id}
    if enh_model_id is not None:
        payload["enh_model_id"] = enh_model_id
    if sources is not None:
        payload["sources"] = _merge_sources(sources)
    return enqueue(session, type_, payload, [model.catalog_id], after)


def copy_job(session: Session, ds: Dataset, model: Model) -> tuple[EnhancedCopy, Job | None]:
    """The enhanced copy of a dataset a plan needs, and the job building it (None when it is ready).

    A ready copy of the build the catalog runs now (or of any build, when the catalog cannot say) is reused;
    so is one being built; otherwise a new copy and its `enhance` job are queued."""
    try:
        rec = catalog.record(model.catalog_id)
    except catalog.CatalogUnavailable:
        rec = None
    current = (rec or {}).get("build_key")
    ready = enhance.find(session, ds.id, model.id, ("ready",), current)
    if ready is not None:
        return ready, None
    building = enhance.find(session, ds.id, model.id, ("building",))
    if building is not None and building.job_id:
        job = session.get(Job, building.job_id)
        if job is not None and job.status in (JobStatus.queued, JobStatus.waiting, JobStatus.running):
            return building, job
    c = enhance.new_copy(session, ds, model, {"build_key": current, "image": (rec or {}).get("image")}, None)
    job = enqueue(session, "enhance", {"dataset_id": ds.id, "model_id": model.id, "copy_id": c.id}, [model.catalog_id])
    c.job_id = job.id
    session.flush()
    return c, job


def read_log(job_id: int, tail: int = 400) -> list[str]:
    p = paths.job_log(job_id)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]


def holder_for(job_id: int) -> str:
    """The catalog lease holder of a job."""
    return f"{catalog.APP}:job-{job_id}"


# ---------------------------------------------------------------- state changes

RUN_JOBS = ("run",)  # job types whose payload names a `run` that mirrors the job's state


def _mirror_run(session: Session, job: Job, status: str, error: str | None = None) -> None:
    if job.type in RUN_JOBS:
        run = session.get(Run, (job.payload or {}).get("run_id"))
        if run is not None:
            run.status, run.error = status, error


def lease_info(d: dict[str, Any]) -> dict[str, Any]:
    return {"id": d.get("id"), "state": d.get("state"), "position": d.get("position"),
            "waiting_for": d.get("waiting_for"), "models": d.get("models")}


def lease_note(d: dict[str, Any]) -> str:
    if d.get("state") == "starting":
        return f"starting {', '.join(d.get('models') or [])} in the Model Catalog"
    pos = f" #{d['position']}" if d.get("position") else ""
    return f"waiting for compute{pos}" + (f": {d['waiting_for']}" if d.get("waiting_for") else "")


def _block(session: Session, job: Job, msg: str, ctx: JobContext | None = None) -> None:
    ctx = ctx or JobContext(job.id)
    ctx.log(f"blocked: {msg}")
    job.status, job.error, job.finished_at, job.lease = JobStatus.blocked, msg, utcnow(), None
    job.message = ctx.message[:300] or None
    _mirror_run(session, job, "blocked", msg)
    session.commit()


def recover(session: Session) -> int:
    """After a crash: running and waiting jobs go back to the queue (their leases expire on their own)."""
    res = session.execute(update(Job).where(Job.status.in_((JobStatus.running, JobStatus.waiting)))
                          .values(status=JobStatus.queued, lease=None))
    return res.rowcount or 0


# ---------------------------------------------------------------- scheduling

def _deps(session: Session, job: Job) -> tuple[str, str | None]:
    """ready | pending | failed (with why) for the jobs `job` waits on."""
    pending = False
    for jid in job.after or []:
        dep = session.get(Job, jid)
        if dep is None or dep.status == JobStatus.done:
            continue
        if dep.status in (JobStatus.failed, JobStatus.blocked):
            return "failed", f"needs job #{dep.id} ({dep.type}), which is {dep.status}: {dep.error or ''}".strip()
        pending = True
    return ("pending", None) if pending else ("ready", None)


def _model_states() -> dict[str, str]:
    try:
        return {k: r.get("state") or "" for k, r in catalog.records().items()}
    except catalog.CatalogUnavailable:
        return {}


def _poll_waiting(session: Session) -> tuple[Job, Lease] | None:
    """Refresh this worker's waiting leases; the first job whose lease became active, claimed."""
    for job_id, lease in list(_waiting.items()):
        job = session.get(Job, job_id)
        if job is None or job.status != JobStatus.waiting:
            _waiting.pop(job_id, None)
            lease.release()
            continue
        try:
            lease.refresh()
        except Exception as exc:
            if not catalog.unavailable(exc):
                raise
        d = lease.data
        if lease.active:
            _waiting.pop(job_id, None)
            job.status, job.lease, job.started_at = JobStatus.running, None, utcnow()
            _mirror_run(session, job, "running")
            session.commit()
            return job, lease
        if d.get("state") in LEASE_DONE or lease.lost:
            _waiting.pop(job_id, None)
            lease.release()
            _block(session, job, f"lease {d.get('state')}: {d.get('error') or lease.lost or d.get('waiting_for') or ''}"
                   .rstrip(": "))
            continue
        info = lease_info(d)
        if info != job.lease:
            job.lease, job.message = info, lease_note(d)[:300]
            session.commit()
            JobContext(job.id).log(lease_note(d))
    return None


def claim_next(session: Session) -> tuple[Job, Lease | None] | None:
    """The next job to run now, with its lease when it is one that waited; None when nothing can run yet.

    A waiting job whose lease has come through goes first. Then, among queued jobs whose earlier stages are
    done, the oldest whose models are all running (or that needs none), else the oldest. While a lease
    waits, only jobs that need no model are claimed."""
    got = _poll_waiting(session)
    if got is not None:
        return got
    cands = []
    for j in session.scalars(select(Job).where(Job.status == JobStatus.queued).order_by(Job.id)).all():
        state, why = _deps(session, j)
        if state == "failed":
            _block(session, j, why or "an earlier stage failed")
        elif state == "ready" and not (_waiting and j.required_services):
            cands.append(j)
    if not cands:
        return None
    states = _model_states() if any(j.required_services for j in cands) else {}
    job = next((j for j in cands if all(states.get(n) == "running" for n in j.required_services)), cands[0])
    job.status, job.started_at, job.attempts = JobStatus.running, utcnow(), job.attempts + 1
    session.commit()
    return job, None


def _take_lease(session: Session, job: Job, ctx: JobContext) -> Lease | None:
    """Check the job's models with the catalog, narrow them (stage jobs with nothing missing need none)
    and ask for a lease. None when the job needs no model."""
    needed = list(job.required_services or [])
    if not needed:
        return None
    try:
        catalog.records(refresh=True)
        for n in needed:
            rec = catalog.record(n)
            why = catalog.unusable_reason(rec, n)
            if why:
                raise JobBlocked(why)
            catalog.ensure_model(session, n, rec)  # the row follows the catalog's current capabilities
        session.commit()
    except catalog.CatalogUnavailable as exc:
        raise JobBlocked(str(exc)) from None
    prepare = PREPARE.get(job.type)
    if prepare is not None:
        needed = prepare(session, job, ctx)
        if not needed:
            return None
    ctx.log(f"leasing {', '.join(needed)} from the Model Catalog as {holder_for(job.id)}")
    try:
        lease = catalog.request_lease(holder_for(job.id), needed)
    except Exception as exc:
        if catalog.unavailable(exc):
            raise JobBlocked(f"the Model Catalog refused the lease: {exc}") from None
        raise
    if lease.data.get("state") in LEASE_DONE:
        lease.release()
        raise JobBlocked(f"lease {lease.data.get('state')}: {lease.data.get('error') or 'no reason given'}")
    return lease


def _wait(session: Session, job: Job, lease: Lease, ctx: JobContext) -> None:
    note = lease_note(lease.data)
    ctx.log(note)
    job.status, job.lease, job.message = JobStatus.waiting, lease_info(lease.data), note[:300]
    _mirror_run(session, job, "waiting")
    session.commit()
    _waiting[job.id] = lease


# ---------------------------------------------------------------- builds

def _current_build(model_id: str) -> dict[str, Any]:
    """The build the catalog runs for a model now: {build_key, image, hardware}."""
    try:
        rec = catalog.record(model_id, refresh=True)
    except catalog.CatalogUnavailable as exc:
        raise JobBlocked(str(exc)) from None
    if rec is None:
        raise JobBlocked(f"{model_id} is not in the Model Catalog")
    if not rec.get("build_key"):
        raise JobBlocked(f"the catalog has no build key for {model_id} yet (its weights are still being hashed, "
                         "or its image is missing); retry shortly")
    return {"build_key": rec["build_key"], "image": rec.get("image"), "hardware": catalog.hardware(rec)}


def _dep_builds(session: Session, job: Job) -> executor.Builds:
    """The builds the earlier stages of a grid used, from their results (with the hardware they ran on)."""
    builds: executor.Builds = {}
    for jid in job.after or []:
        dep = session.get(Job, jid)
        r = (dep.result or {}) if dep is not None else {}
        if r.get("model") and r.get("build_key"):
            hw = dict(r.get("hardware") or {})
            if r.get("computed") == 0:
                hw["reused"] = True  # every output was stored already: nothing ran on this hardware now
            builds[r["model"]] = {"build_key": r["build_key"], "image": r.get("image"), "hardware": hw}
    return builds


def _dep_seconds(session: Session, job: Job) -> float:
    """Seconds the earlier stages of a grid took, start to finish."""
    total = 0.0
    for jid in job.after or []:
        dep = session.get(Job, jid)
        if dep is not None and dep.started_at and dep.finished_at:
            total += (dep.finished_at - dep.started_at).total_seconds()
    return total


def _stage_subject(session: Session, job: Job) -> tuple[Model, Dataset]:
    model = session.get(Model, job.payload["model_id"])
    ds = session.get(Dataset, job.payload["dataset_id"])
    if model is None or ds is None:
        raise ValueError("the stage's model or dataset no longer exists")
    return model, ds


def _sources(session: Session, job: Job) -> list[executor.Source]:
    out = []
    for src in job.payload.get("sources") or []:
        spec = source_spec(src)
        seg = session.get(Model, spec["seg"]) if spec.get("seg") is not None else None
        enh = session.get(Model, spec["seg_enh"]) if spec.get("seg_enh") is not None else None
        out.append(executor.Source(seg, enh))
    return out


def _enh_model(session: Session, job: Job) -> Model | None:
    i = (job.payload or {}).get("enh_model_id")
    return session.get(Model, i) if i is not None else None


# ---------------------------------------------------------------- handlers

def _handle_ingest(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    from ..datasets.ingest import ingest

    p = job.payload
    source = Path(p["source"])
    imp_id = p.get("reference_import_id")
    try:
        report = ingest(session, p["dataset_id"], source, ctx.progress)
        session.commit()
        if imp_id:
            from ..datasets import refpackage

            refpackage.finish(session, imp_id, p["dataset_id"], ctx.log)
    except Exception as exc:
        session.rollback()
        ds = session.get(Dataset, p["dataset_id"])
        if ds is not None:
            ds.status = "failed"
            session.commit()
        if imp_id:
            from ..datasets import refpackage

            refpackage.fail(session, imp_id, str(exc))
        raise
    if p.get("cleanup") and source.is_relative_to(paths.data_root() / "uploads"):
        shutil.rmtree(source, ignore_errors=True)
    ctx.log(f"ingested {report['files']} files ({report['hours'] * 60:.1f} min), {report['usable_files']} usable")
    if p.get("plan"):
        from ..api.benchmarks import enqueue_plan

        try:
            jobs = enqueue_plan(session, p["dataset_id"], p["plan"])
            ctx.log(f"benchmark plan: queued {len(jobs)} job(s)")
        except Exception as exc:  # noqa: BLE001 - the dataset is in; the plan can be run from its page
            session.rollback()
            ctx.log(f"benchmark plan not queued: {exc}")
    return {"dataset_id": p["dataset_id"]}


def _handle_run(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    run = session.get(Run, job.payload["run_id"])
    if run is None:
        raise ValueError(f"run {job.payload['run_id']} not found")
    assemble = bool(job.payload.get("assemble"))
    try:
        builds = _dep_builds(session, job)
        for m in executor.models_of(run):
            if m.catalog_id not in builds:
                builds[m.catalog_id] = _current_build(m.catalog_id)
        if assemble:
            ctx.log("assembling the pipeline from its stages' stored outputs")
        executor.execute(session, run, ctx.log, lambda p, m: ctx.progress(0.9 * p, m), builds,
                         infer_missing=not assemble)
        run.status = "done"
        if run.started_at and run.finished_at:
            run.wall_s = round((run.finished_at - run.started_at).total_seconds()
                               + (_dep_seconds(session, job) if assemble else 0.0), 3)
        session.commit()
    except JobBlocked:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        run = session.get(Run, job.payload["run_id"])
        run.status, run.error = "failed", str(exc)[:2000]
        session.commit()
        raise
    ctx.progress(0.92, "scoring")
    score_ids = []
    for n in job.payload.get("normalisers") or [None]:
        try:
            sc = scoring.score_run(session, run, n, job.payload.get("collar"))
            score_ids.append(sc.id)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - keep the inference even if one normaliser fails
            session.rollback()
            ctx.log(f"scoring with {n or 'default'} failed: {exc}")
    return {"run_id": run.id, "score_ids": score_ids}


def _prepare_seg_stage(session: Session, job: Job, ctx: JobContext) -> list[str]:
    model, ds = _stage_subject(session, job)
    rec = catalog.record(model.catalog_id)
    if not (rec or {}).get("build_key"):
        return list(job.required_services)
    st = executor.seg_stage(session, model, ds, {"build_key": rec["build_key"], "image": rec.get("image")},
                            ctx.log, ctx.progress, mode="check", enh_model=_enh_model(session, job))
    return _needed(job, ctx, model, st)


def _prepare_asr_stage(session: Session, job: Job, ctx: JobContext) -> list[str]:
    model, ds = _stage_subject(session, job)
    rec = catalog.record(model.catalog_id)
    if not (rec or {}).get("build_key"):
        return list(job.required_services)
    st = executor.asr_stage(session, model, ds, _sources(session, job), _dep_builds(session, job),
                            {"build_key": rec["build_key"], "image": rec.get("image")}, ctx.log, ctx.progress,
                            mode="check", enh_model=_enh_model(session, job))
    return _needed(job, ctx, model, st)


def _prepare_enhance(session: Session, job: Job, ctx: JobContext) -> list[str]:
    c = session.get(EnhancedCopy, job.payload["copy_id"])
    if c is not None and c.status == "ready":
        ctx.log("the enhanced copy is ready already; no lease needed")
        return []
    return list(job.required_services)


def _needed(job: Job, ctx: JobContext, model: Model, st: dict[str, Any]) -> list[str]:
    if st["missing"] == 0:
        ctx.log(f"{model.catalog_id}: all {st['calls']} outputs stored already ({st['copied']} read from other apps); "
                "no lease needed")
        return []
    ctx.log(f"{model.catalog_id}: {st['missing']} of {st['calls']} outputs to compute")
    return list(job.required_services)


def _handle_seg_stage(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    model, ds = _stage_subject(session, job)
    return executor.seg_stage(session, model, ds, _current_build(model.catalog_id), ctx.log, ctx.progress,
                              enh_model=_enh_model(session, job))


def _handle_asr_stage(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    model, ds = _stage_subject(session, job)
    return executor.asr_stage(session, model, ds, _sources(session, job), _dep_builds(session, job),
                              _current_build(model.catalog_id), ctx.log, ctx.progress, enh_model=_enh_model(session, job))


def _handle_enhance(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    from .. import adapters

    model, ds = _stage_subject(session, job)
    c = session.get(EnhancedCopy, job.payload["copy_id"])
    if c is None:
        raise ValueError("the enhanced copy no longer exists")
    if c.status != "ready":
        build = _current_build(model.catalog_id)
        c.status, c.error = "building", None
        c.build_key, c.image, c.hardware = build["build_key"], build.get("image"), build.get("hardware") or {}
        session.commit()
        try:
            enhance.build(session, c, adapters.for_model(model), ctx.log, ctx.progress)
        except enhance.Cancelled:
            session.rollback()
            c = session.get(EnhancedCopy, job.payload["copy_id"])
            enhance.remove_audio(session, c)
            c.status, c.finished_at = "cancelled", utcnow()
            session.commit()
            raise
        except Exception as exc:
            session.rollback()
            c = session.get(EnhancedCopy, job.payload["copy_id"])
            c.status, c.error, c.finished_at = "failed", str(exc)[:2000], utcnow()
            session.commit()
            raise
        if c.status != "ready":
            raise executor.RunError(c.error or "the enhanced copy failed")
    computed = sum(1 for _ in enhance.files_of(session, c))
    return {"model": model.catalog_id, "build_key": c.build_key, "image": c.image, "hardware": c.hardware or {},
            "copy_id": c.id, "files": c.files, "computed": computed}


def _handle_score(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    run = session.get(Run, job.payload["run_id"])
    sc = scoring.score_run(session, run, job.payload.get("normaliser"), job.payload.get("collar"),
                           bool(job.payload.get("skip_overlap")))
    session.commit()
    return {"run_id": run.id, "score_id": sc.id}


HANDLERS = {"ingest": _handle_ingest, "run": _handle_run, "seg_stage": _handle_seg_stage,
            "asr_stage": _handle_asr_stage, "score": _handle_score, "enhance": _handle_enhance}
# Before leasing: the models still needed once stored and indexed outputs are counted.
PREPARE = {"seg_stage": _prepare_seg_stage, "asr_stage": _prepare_asr_stage, "enhance": _prepare_enhance}


def run_job(job_id: int, lease: Lease | None = None) -> None:
    """Run a claimed job. Without a lease, take one first; if the catalog queues it, the job is left
    `waiting` and `claim_next` comes back to it."""
    ctx = JobContext(job_id)
    session = SessionLocal()
    keep_lease = False
    try:
        job = session.get(Job, job_id)
        assert job is not None
        if lease is None:
            lease = _take_lease(session, job, ctx)
            if lease is not None and not lease.active:
                _wait(session, job, lease, ctx)
                keep_lease = True
                return
        if lease is not None:
            ctx.log(f"lease {lease.id} active: {', '.join(lease.models)}")
        result = HANDLERS[job.type](session, job, ctx)
        job = session.get(Job, job_id)
        job.status, job.result, job.progress, job.finished_at = JobStatus.done, result, 1.0, utcnow()
        job.message = ctx.message[:300] or None
        session.commit()
    except JobBlocked as exc:
        session.rollback()
        _block(session, session.get(Job, job_id), str(exc), ctx)
    except Exception as exc:  # noqa: BLE001 - recorded on the job
        session.rollback()
        ctx.log("failed: " + "".join(traceback.format_exception_only(exc)).strip())
        with open(ctx.log_path, "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc())
        job = session.get(Job, job_id)
        job.status, job.error, job.finished_at = JobStatus.failed, str(exc)[:2000], utcnow()
        job.message = ctx.message[:300] or None
        session.commit()
    finally:
        if lease is not None and not keep_lease:
            peaks = catalog.peak_vram(lease)  # read while the lease is live: the catalog's running peak
            try:
                lease.release()
            except Exception as exc:  # noqa: BLE001 - the job's outcome is already recorded
                ctx.log(f"release failed: {exc}")
            _record_peak(session, job_id, peaks)
        session.close()


def _record_peak(session: Session, job_id: int, peaks: dict[str, float]) -> None:
    """Peak VRAM the job's lease measured (platform P1): onto the run's hardware, or a stage job's result."""
    if not peaks:
        return
    try:
        session.rollback()
        job = session.get(Job, job_id)
        if job is None or job.status != JobStatus.done:
            return
        if job.type in RUN_JOBS:
            run = session.get(Run, job.payload["run_id"])
            if run is not None:
                run.hardware = {k: ({**v, "peak_vram_gb": peaks[k]} if k in peaks else v)
                                for k, v in (run.hardware or {}).items()}
        elif job.result.get("model") in peaks:
            job.result = {**job.result, "hardware": {**(job.result.get("hardware") or {}),
                                                     "peak_vram_gb": peaks[job.result["model"]]}}
        session.commit()
    except Exception:  # noqa: BLE001 - best effort; the job is done either way
        session.rollback()


def run_pending(max_jobs: int | None = None, poll_s: float = 0.5) -> int:
    """Run queued jobs in this process until none is left (tests, `concord worker --once`); with jobs
    waiting for compute, keep polling their leases until they have run."""
    done = 0
    while max_jobs is None or done < max_jobs:
        session = SessionLocal()
        try:
            claimed = claim_next(session)
        finally:
            session.close()
        if claimed is None:
            if max_jobs is None and _waiting:
                time.sleep(poll_s)
                continue
            break
        job, lease = claimed
        run_job(job.id, lease)
        done += 1
    return done


def retry(session: Session, job: Job) -> None:
    """Queue a failed or blocked job again, with the later stages it blocked."""
    job.status, job.error, job.progress, job.message, job.lease = JobStatus.queued, None, 0.0, None, None
    if job.type in RUN_JOBS:
        run = session.get(Run, job.payload["run_id"])
        if run is not None:
            run.status, run.error = "queued", None
    if job.type == "enhance":
        c = session.get(EnhancedCopy, job.payload["copy_id"])
        if c is not None and c.status in ("failed", "cancelled"):
            c.status, c.error, c.progress = "building", None, 0.0
    for dep in session.scalars(select(Job).where(Job.status == JobStatus.blocked)).all():
        if job.id in (dep.after or []):
            retry(session, dep)
