"""The job queue: one worker process reading the `job` table (no Redis / Celery).

Job types:
- ingest  {dataset_id, source, cleanup, plan?, reference_import_id?}
                                                copy, decode, derive, parse references; for a reference
                                                package, then its uids and seg-model outputs
- check   {service}                             compatibility check on the check clip
- run     {run_id, normalisers?}                inference, then scoring with the default normaliser / collar
- score   {run_id, normaliser, collar}          rescoring that needs a platform service (text.transform)
- export  {export_id, services, kind, dest, ...}  an offline kit; needs no service

Before a job needs services, the worker starts them with the platform's `stack.py`. It batches: the
services of later queued jobs that fit next to this job's under `util_limit` are started in the
same call, which keeps eviction to the minimum. If the platform tool fails, the job is `blocked`
with its message. The services are leased to `bench:job-<id>` while the job runs, renewed every minute
and released when it ends (`services.Lease`), so Concord Label's worker never stops them mid-run.
"""

from __future__ import annotations

import json
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core import paths
from ..core.db import SessionLocal
from ..core.models import Dataset, Job, JobStatus, Model, OfflineExport, Run, utcnow
from ..platform import services
from . import executor, scoring


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


def enqueue(session: Session, type_: str, payload: dict[str, Any], required_services: list[str] | None = None) -> Job:
    job = Job(type=type_, payload=payload, required_services=sorted(set(required_services or [])))
    session.add(job)
    session.flush()
    return job


def read_log(job_id: int, tail: int = 400) -> list[str]:
    p = paths.job_log(job_id)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]


# ---------------------------------------------------------------- scheduling

def recover(session: Session) -> int:
    """After a crash: running jobs go back to the queue."""
    res = session.execute(update(Job).where(Job.status == JobStatus.running).values(status=JobStatus.queued))
    return res.rowcount or 0


def _up(names: set[str]) -> set[str]:
    return {n for n in names if services.health(n).up}


def claim_next(session: Session) -> tuple[Job, list[str]] | None:
    """The next job and the service set to ensure for it (its own plus batch-mates')."""
    queued = session.scalars(select(Job).where(Job.status == JobStatus.queued).order_by(Job.id)).all()
    if not queued:
        return None
    wanted = {n for j in queued for n in j.required_services}
    up = _up(wanted) if wanted else set()
    # Prefer the oldest job whose services are all up: no start, no eviction.
    job = next((j for j in queued if set(j.required_services) <= up), queued[0])
    batch = set(job.required_services)
    if batch - up:
        for other in queued:
            cand = batch | set(other.required_services)
            if other.id != job.id and services.fits_together(cand):
                batch = cand
    job.status, job.started_at, job.attempts = JobStatus.running, utcnow(), job.attempts + 1
    session.commit()
    return job, sorted(batch)


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

        jobs = enqueue_plan(session, p["dataset_id"], p["plan"])
        ctx.log(f"benchmark plan: queued {len(jobs)} run(s)")
    return {"dataset_id": p["dataset_id"]}


def _handle_check(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    from ..registry.check import run_check

    rep = run_check(job.payload["service"], ctx.log)
    for line in rep.lines:
        ctx.log(f"[{line.status}] {line.name}: {line.reason}")
    report = rep.to_json()
    # Re-checking a registered model refreshes what it stores.
    m = session.scalar(select(Model).where(Model.registry_name == rep.service))
    if m is not None and job.payload.get("update_model"):
        m.check_report, m.capabilities, m.timing_level = report, rep.capabilities, rep.timing_level
    return {"report": report}


def _handle_run(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    run = session.get(Run, job.payload["run_id"])
    if run is None:
        raise ValueError(f"run {job.payload['run_id']} not found")
    try:
        executor.execute(session, run, ctx.log, lambda p, m: ctx.progress(0.9 * p, m))
        run.status = "done"
        session.commit()
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


def _handle_score(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    run = session.get(Run, job.payload["run_id"])
    sc = scoring.score_run(session, run, job.payload.get("normaliser"), job.payload.get("collar"))
    session.commit()
    return {"run_id": run.id, "score_id": sc.id}


def _handle_export(session: Session, job: Job, ctx: JobContext) -> dict[str, Any]:
    from ..offline import export as offline

    p = job.payload
    rec = session.get(OfflineExport, p["export_id"])
    if rec is None:
        raise ValueError(f"offline export {p['export_id']} not found")
    rec.status, rec.error = "running", None
    session.commit()
    try:
        # jobs queued before kits could carry both apps name one image of this app
        apps = p["apps"] if "apps" in p else {offline.THIS_APP: p["concord_image"]} if p.get("concord_image") else None
        out = offline.export(p["services"], p["kind"], Path(p["dest"]), apps, p.get("name"),
                             ctx.log, ctx.progress)
    except Exception as exc:
        session.rollback()
        rec = session.get(OfflineExport, p["export_id"])
        rec.status, rec.error, rec.finished_at = "failed", str(exc)[:2000], utcnow()
        session.commit()
        raise
    rec.status, rec.path, rec.bytes, rec.finished_at = "done", out["path"], out["bytes"], utcnow()
    session.commit()
    return {"export_id": rec.id, "path": out["path"], "bytes": out["bytes"]}


HANDLERS = {"ingest": _handle_ingest, "check": _handle_check, "run": _handle_run, "score": _handle_score,
            "export": _handle_export}
RUN_JOBS = ("run",)  # job types whose payload names a `run` that mirrors the job's state


def holder_for(job_id: int) -> str:
    """The platform lease holder of a job (SPLIT_PLAN.md §6)."""
    return f"bench:job-{job_id}"


def run_job(job_id: int, batch: list[str] | None = None) -> None:
    ctx = JobContext(job_id)
    session = SessionLocal()
    lease: services.Lease | None = None
    try:
        job = session.get(Job, job_id)
        assert job is not None
        needed = batch or job.required_services
        if needed:
            lease = services.Lease(needed, holder_for(job_id), ctx.log)
            try:
                lease.acquire()
            except services.ServicesBlocked as exc:
                ctx.log(f"blocked: {exc}")
                job.status, job.error, job.finished_at = JobStatus.blocked, str(exc), utcnow()
                job.message = ctx.message[:300] or None
                if job.type in RUN_JOBS:
                    run = session.get(Run, job.payload["run_id"])
                    if run is not None:
                        run.status, run.error = "blocked", str(exc)
                session.commit()
                return
        result = HANDLERS[job.type](session, job, ctx)
        job = session.get(Job, job_id)
        job.status, job.result, job.progress, job.finished_at = JobStatus.done, result, 1.0, utcnow()
        job.message = ctx.message[:300] or None
        session.commit()
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
        if lease is not None:
            try:
                lease.close()
            except Exception as exc:  # noqa: BLE001 - the job's outcome is already recorded
                ctx.log(f"release failed: {exc}")
        session.close()


def run_pending(max_jobs: int | None = None) -> int:
    """Run queued jobs in this process until the queue is empty (tests, `concord worker --once`)."""
    done = 0
    while max_jobs is None or done < max_jobs:
        session = SessionLocal()
        try:
            claimed = claim_next(session)
        finally:
            session.close()
        if claimed is None:
            break
        job, batch = claimed
        run_job(job.id, batch)
        done += 1
    return done


def retry(session: Session, job: Job) -> None:
    job.status, job.error, job.progress, job.message = JobStatus.queued, None, 0.0, None
    if job.type in RUN_JOBS:
        run = session.get(Run, job.payload["run_id"])
        if run is not None:
            run.status, run.error = "queued", None
    if job.type == "export":
        rec = session.get(OfflineExport, job.payload["export_id"])
        if rec is not None:
            rec.status, rec.error, rec.finished_at = "queued", None, None
