"""Benchmark tables (model x dataset), the pipeline ablation grid, benchmark plans and suggestions.

Every number returned here comes from a `score` row; missing scores for a done run are computed on
request (seconds, no inference).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analysis.suggest import rank_models
from ..core.db import get_session
from ..core.models import Dataset, Job, Model, Pipeline, Run, RunTask, Score
from ..metrics import normalisers
from ..metrics.der import DEFAULT_COLLAR
from .datasets import job_brief
from .runs import enqueue_run, ensure_score, get_or_create_pipeline, run_label

router = APIRouter(tags=["benchmarks"])


def _ds_json(d: Dataset, has_labels: bool) -> dict[str, Any]:
    rep = d.report or {}
    return {"id": d.id, "name": d.name, "tags": d.tags, "label_types": d.label_types, "hours": rep.get("usable_hours"),
            "files": rep.get("usable_files"), "has_labels": has_labels, "status": d.status,
            "has_speaker_text": bool(rep.get("has_speaker_text")), "language": d.language}


def _runs_by_key(session: Session, task: str) -> dict[tuple, list[Run]]:
    out: dict[tuple, list[Run]] = {}
    for r in session.scalars(select(Run).where(Run.task == task).order_by(Run.id)):
        key = (r.pipeline_id, r.dataset_id) if task == RunTask.pipeline else (r.model_id, r.dataset_id)
        out.setdefault(key, []).append(r)
    return out


def _value(task_tab: str, agg: dict[str, Any], speaker_model: bool) -> tuple[float | None, str]:
    if task_tab == "asr":
        return agg.get("wer"), "WER"
    if speaker_model and agg.get("der") is not None:
        return agg.get("der"), "DER"
    return agg.get("vad_error"), "VAD error"


def _breakdown(task_tab: str, agg: dict[str, Any], metric: str) -> dict[str, Any]:
    if task_tab == "asr":
        n = agg.get("n") or 0
        return {"parts": [{"key": k, "label": lbl, "count": agg.get(k), "pct": (agg.get(k) or 0) / n if n else None}
                          for k, lbl in (("s", "Substitutions"), ("d", "Deletions"), ("i", "Insertions"))],
                "facts": {"Words (N)": n, "CER": agg.get("cer"), "Utterances": agg.get("utterances"),
                          "With errors": agg.get("with_errors")}}
    if metric == "DER":
        tot = agg.get("total") or 0
        return {"parts": [{"key": k, "label": lbl, "seconds": agg.get(k), "pct": (agg.get(k) or 0) / tot if tot
                           else None} for k, lbl in (("missed", "Missed speech"), ("false_alarm", "False alarm"),
                                                     ("confusion", "Speaker confusion"))],
                "facts": {"JER": agg.get("jer"), "Speaker count right": agg.get("speaker_count_acc"),
                          "Reference speech (s)": tot, "Files": agg.get("files")}}
    tot = agg.get("vad_total") or 0
    return {"parts": [{"key": k, "label": lbl, "seconds": agg.get(k), "pct": (agg.get(k) or 0) / tot if tot else None}
                      for k, lbl in (("vad_missed", "Missed speech"), ("vad_false_alarm", "False alarm"))],
            "facts": {"Reference speech (s)": tot, "Files": agg.get("files")}}


@router.get("/benchmarks/{tab}")
def benchmark_table(tab: str, normaliser: str | None = None, collar: float | None = None,
                    session: Session = Depends(get_session)) -> dict[str, Any]:
    if tab not in ("asr", "diar"):
        raise HTTPException(404, "tab must be asr or diar (pipelines: /benchmarks-pipelines)")
    try:
        nkey = normalisers.get(normaliser or normalisers.DEFAULT).key
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from None
    collar = DEFAULT_COLLAR if collar is None else collar
    run_task = RunTask.asr if tab == "asr" else RunTask.seg
    label = "asr" if tab == "asr" else "diar"
    datasets = list(session.scalars(select(Dataset).order_by(Dataset.name)))
    models = [m for m in session.scalars(select(Model).order_by(Model.display_name))
              if (m.is_asr if tab == "asr" else m.is_seg)]
    runs = _runs_by_key(session, run_task)
    cells = []
    for m in models:
        for d in datasets:
            rs = runs.get((m.id, d.id), [])
            done = [r for r in rs if r.status == "done"]
            latest = rs[-1] if rs else None
            cell: dict[str, Any] = {"model_id": m.id, "dataset_id": d.id, "status": latest.status if latest else None,
                                    "run_id": None, "value": None, "prev": None, "delta": None}
            if latest and latest.status in ("failed", "blocked"):
                cell["error"] = latest.error
            if done:
                cur = done[-1]
                sc, job = ensure_score(session, cur, nkey, collar)
                cell["run_id"] = cur.id
                if job is not None:
                    cell["pending_score_job"] = job.id
                if sc is not None:
                    v, metric = _value(tab, sc.aggregates, m.has_speakers)
                    cell.update({"value": v, "metric": metric, "score_id": sc.id,
                                 "breakdown": _breakdown(tab, sc.aggregates, metric),
                                 "rtf": (cur.rtf_by_stage or {}).get("total"),
                                 "finished_at": cur.finished_at.isoformat() if cur.finished_at else None,
                                 "image": next(iter(cur.image_tags.values()), {}).get("image") if cur.image_tags
                                 else None})
                    if len(done) > 1:
                        psc, _ = ensure_score(session, done[-2], nkey, collar, queue=False)
                        if psc is not None:
                            pv, _ = _value(tab, psc.aggregates, m.has_speakers)
                            cell["prev"] = pv
                            cell["delta"] = v - pv if v is not None and pv is not None else None
                if latest is not cur and latest.status in ("queued", "running"):
                    cell["rerunning"] = True
            cells.append(cell)
    recent = list(session.scalars(select(Run).where(Run.task == run_task).order_by(Run.id.desc()).limit(8)))
    return {
        "tab": tab, "normaliser": nkey, "collar": collar,
        "datasets": [_ds_json(d, label in (d.label_types or []) and d.status == "ready") for d in datasets],
        "models": [{"id": m.id, "display_name": m.display_name, "registry_name": m.registry_name, "tags": m.tags,
                    "capabilities": m.capabilities, "timing_level": m.timing_level,
                    "kind": "asr" if tab == "asr" else ("diar" if m.has_speakers else "vad")} for m in models],
        "cells": cells,
        "recent_runs": [{"id": r.id, "label": run_label(r), "dataset": r.dataset.name, "status": r.status,
                         "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in recent],
    }


@router.get("/benchmarks-pipelines")
def pipeline_grid(dataset_id: int | None = None, normaliser: str | None = None, collar: float | None = None,
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    nkey = normalisers.get(normaliser or normalisers.DEFAULT).key
    collar = DEFAULT_COLLAR if collar is None else collar
    datasets = list(session.scalars(select(Dataset).order_by(Dataset.name)))
    eligible = [d for d in datasets if "asr" in (d.label_types or []) and d.status == "ready"]
    ds = session.get(Dataset, dataset_id) if dataset_id else (eligible[0] if eligible else None)
    models = list(session.scalars(select(Model).order_by(Model.display_name)))
    seg_models = [m for m in models if m.is_seg]
    asr_models = [m for m in models if m.is_asr]
    out: dict[str, Any] = {
        "normaliser": nkey, "collar": collar, "dataset_id": ds.id if ds else None,
        "datasets": [_ds_json(d, d in eligible) for d in datasets],
        "rows": ([{"seg_model_id": None, "name": "Reference segments + speakers", "oracle": True,
                   "has_speakers": True, "available": bool(ds and "diar" in (ds.label_types or []))}] +
                 [{"seg_model_id": m.id, "name": m.display_name, "oracle": False, "has_speakers": m.has_speakers,
                   "available": True} for m in seg_models]),
        "cols": [{"asr_model_id": m.id, "name": m.display_name, "timing_level": m.timing_level} for m in asr_models],
        "cells": [], "runs": [],
    }
    if ds is None:
        return out
    pipes = {(p.seg_model_id, p.asr_model_id): p for p in session.scalars(select(Pipeline))}
    runs = _runs_by_key(session, RunTask.pipeline)
    for row in out["rows"]:
        for col in out["cols"]:
            p = pipes.get((row["seg_model_id"], col["asr_model_id"]))
            rs = runs.get((p.id, ds.id), []) if p else []
            done = [r for r in rs if r.status == "done"]
            cell: dict[str, Any] = {"seg_model_id": row["seg_model_id"], "asr_model_id": col["asr_model_id"],
                                    "pipeline_id": p.id if p else None, "status": rs[-1].status if rs else None,
                                    "run_id": None, "has_speakers": row["has_speakers"], "oracle": row["oracle"]}
            if rs and rs[-1].status in ("failed", "blocked"):
                cell["error"] = rs[-1].error
            if done:
                cur = done[-1]
                sc, job = ensure_score(session, cur, nkey, collar)
                cell["run_id"] = cur.id
                if job is not None:
                    cell["pending_score_job"] = job.id
                if sc is not None:
                    a = sc.aggregates
                    cell.update({
                        "score_id": sc.id, "wer": (a.get("wer") or {}).get("wer"),
                        "cpwer": (a.get("cpwer") or {}).get("cpwer") if a.get("cpwer") else None,
                        "der": (a.get("der") or {}).get("der") if a.get("der") else None,
                        "rtf": (cur.rtf_by_stage or {}).get("total"), "rtf_by_stage": cur.rtf_by_stage,
                        "attribution": a.get("attribution"), "wrong_speaker_pairs": a.get("wrong_speaker_pairs"),
                        "label": run_label(cur)})
                    if len(done) > 1:
                        psc, _ = ensure_score(session, done[-2], nkey, collar, queue=False)
                        if psc is not None and psc.aggregates.get("cpwer") and cell["cpwer"] is not None:
                            cell["prev_cpwer"] = psc.aggregates["cpwer"]["cpwer"]
            out["cells"].append(cell)
    ranked = [c for c in out["cells"] if c.get("score_id")]
    ranked.sort(key=lambda c: (c["cpwer"] is None, c["cpwer"] if c["cpwer"] is not None else c["wer"] or 9e9))
    out["runs"] = ranked
    out["dataset"] = _ds_json(ds, ds in eligible)
    return out


# ---------------------------------------------------------------- plans

class PlanIn(BaseModel):
    dataset_id: int
    seg_model_ids: list[int] = Field(default_factory=list)
    asr_model_ids: list[int] = Field(default_factory=list)
    oracle: bool = False
    mode: str = "grid"  # grid: every seg x every ASR as pipelines; stages: each model alone


def plan_size(seg: int, asr: int, oracle: bool, mode: str) -> int:
    return (seg + int(oracle)) * asr if mode == "grid" else seg + asr


def enqueue_plan(session: Session, dataset_id: int, plan: dict[str, Any]) -> list[Job]:
    ds = session.get(Dataset, dataset_id)
    if ds is None or ds.status != "ready":
        return []
    seg = [m for m in (session.get(Model, i) for i in plan.get("seg_model_ids") or []) if m and m.is_seg]
    asr = [m for m in (session.get(Model, i) for i in plan.get("asr_model_ids") or []) if m and m.is_asr]
    oracle = bool(plan.get("oracle")) and "diar" in (ds.label_types or [])
    out: list[Job] = []
    if plan.get("mode", "grid") == "grid" and "asr" in (ds.label_types or []):
        seg_ids: list[int | None] = ([None] if oracle else []) + [m.id for m in seg]
        for sid in seg_ids:
            for a in asr:
                p = get_or_create_pipeline(session, sid, a.id)
                out.append(enqueue_run(session, RunTask.pipeline, ds, pipeline=p)[1])
    else:
        if "diar" in (ds.label_types or []):
            out += [enqueue_run(session, RunTask.seg, ds, model=m)[1] for m in seg]
        if "asr" in (ds.label_types or []):
            out += [enqueue_run(session, RunTask.asr, ds, model=m)[1] for m in asr]
    session.commit()
    return out


@router.post("/benchmarks/plan", status_code=202)
def run_plan(body: PlanIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = session.get(Dataset, body.dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    if ds.status != "ready":
        raise HTTPException(409, f"dataset is {ds.status}")
    js = enqueue_plan(session, ds.id, body.model_dump())
    return {"queued": len(js), "jobs": [job_brief(j) for j in js]}


@router.get("/suggestions/models")
def suggest_models(dataset_id: int | None = None, tags: str = "", language: str | None = None,
                   label_types: str = "", session: Session = Depends(get_session)) -> dict[str, Any]:
    """Models grouped by stage, ranked by tag overlap with the dataset (or with the tags being typed)."""
    if dataset_id:
        ds = session.get(Dataset, dataset_id)
        if ds is None:
            raise HTTPException(404, "dataset not found")
        tag_list, language, labels = ds.tags, ds.language, set(ds.label_types or [])
    else:
        tag_list, labels = [t for t in tags.split(",") if t], {t for t in label_types.split(",") if t}
    models = list(session.scalars(select(Model)))
    ranked = rank_models(tag_list, models, language)
    by_id = {m.id: m for m in models}

    def entry(r: dict[str, Any]) -> dict[str, Any]:
        m = by_id[r["model_id"]]
        return {**r, "display_name": m.display_name, "tags": m.tags, "capabilities": m.capabilities,
                "has_speakers": m.has_speakers, "timing_level": m.timing_level}

    return {
        "seg": [entry(r) for r in ranked if "seg" in r["stages"]],
        "asr": [entry(r) for r in ranked if "asr" in r["stages"]],
        "oracle_available": "diar" in labels,
        "labels": sorted(labels),
    }


@router.get("/scores/{score_id}")
def score_detail(score_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    sc = session.get(Score, score_id)
    if sc is None:
        raise HTTPException(404, "score not found")
    return {"id": sc.id, "run_id": sc.run_id, "normaliser": sc.normaliser, "collar": sc.collar,
            "aggregates": sc.aggregates, "label": run_label(sc.run), "dataset_id": sc.run.dataset_id}
