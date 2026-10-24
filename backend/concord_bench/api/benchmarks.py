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
from ..core.models import Dataset, EnhancedCopy, Job, Model, Pipeline, Run, RunTask, Score, Slice
from ..metrics import cpwer, normalisers, wer
from ..metrics.der import DEFAULT_COLLAR
from ..platform import catalog
from ..runs import executor
from .datasets import job_brief
from .models import catalog_listing
from .runs import PLACEMENTS, enqueue_pipelines, enqueue_run, ensure_score, get_or_create_pipeline, run_label

router = APIRouter(tags=["benchmarks"])


def _ds_json(d: Dataset, has_labels: bool) -> dict[str, Any]:
    rep = d.report or {}
    return {"id": d.id, "name": d.name, "tags": d.tags, "label_types": d.label_types, "hours": rep.get("usable_hours"),
            "files": rep.get("usable_files"), "has_labels": has_labels, "status": d.status,
            "has_speaker_text": bool(rep.get("has_speaker_text")), "language": d.language}


def _runs_by_key(session: Session, task: str) -> dict[tuple, list[Run]]:
    """Runs of a task by (model or pipeline, dataset, enhancement model, placement), oldest first."""
    out: dict[tuple, list[Run]] = {}
    for r in session.scalars(select(Run).where(Run.task == task).order_by(Run.id)):
        subject = r.pipeline_id if task == RunTask.pipeline else r.model_id
        out.setdefault((subject, r.dataset_id, r.enh_model_id, r.enh_placement), []).append(r)
    return out


def _enh_json(m: Model) -> dict[str, Any]:
    return {"id": m.id, "display_name": m.display_name, "catalog_id": m.catalog_id, "generative": m.generative}


def _copies(session: Session) -> list[dict[str, Any]]:
    """The newest live copy of each (dataset, enhancement model), for "building copy" cells."""
    seen: dict[tuple[int, int], EnhancedCopy] = {}
    for c in session.scalars(select(EnhancedCopy).where(EnhancedCopy.status.in_(("building", "ready", "failed")))
                             .order_by(EnhancedCopy.id)):
        seen[(c.dataset_id, c.model_id)] = c
    return [{"dataset_id": c.dataset_id, "enh_model_id": c.model_id, "copy_id": c.id, "status": c.status,
             "progress": c.progress, "rtf": c.rtf} for c in seen.values()]


ASR_METRICS = {"wer": "WER", "cer": "CER", "mer": "MER", "wil": "WIL", "wip": "WIP"}
ASR_COUNTS = ("n", "p", "h", "s", "d", "i", "errors", "utterances", "with_errors")


def asr_metrics(agg: dict[str, Any]) -> dict[str, Any]:
    """Every ASR metric and count of a score's aggregates (MER / WIL / WIP derived for older scores)."""
    a = wer.with_info(agg)
    return {k: a.get(k) for k in (*ASR_METRICS, *ASR_COUNTS)}


def _value(task_tab: str, agg: dict[str, Any], speaker_model: bool, metric: str = "wer") -> tuple[float | None, str]:
    if task_tab == "asr":
        return asr_metrics(agg).get(metric), ASR_METRICS[metric]
    if speaker_model and agg.get("der") is not None:
        return agg.get("der"), "DER"
    return agg.get("vad_error"), "VAD error"


def _breakdown(task_tab: str, agg: dict[str, Any], metric: str) -> dict[str, Any]:
    if task_tab == "asr":
        a = asr_metrics(agg)
        n = a.get("n") or 0
        return {"parts": [{"key": k, "label": lbl, "count": a.get(k), "pct": (a.get(k) or 0) / n if n else None}
                          for k, lbl in (("s", "Substitutions"), ("d", "Deletions"), ("i", "Insertions"))],
                "facts": {"Words (N)": n, "CER": a.get("cer"), "Utterances": a.get("utterances"),
                          "With errors": a.get("with_errors")}}
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


def run_compute(run: Run) -> dict[str, Any]:
    """RTF, wall time and hardware of a run, for leaderboard cells and inspectors."""
    return {"rtf": (run.rtf_by_stage or {}).get("total"), "rtf_by_stage": run.rtf_by_stage or {},
            "wall_s": run.wall_s, "audio_s": (run.rtf_by_stage or {}).get("audio_s"), "hardware": run.hardware or {},
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "image": next(iter(run.builds.values()), {}).get("image") if run.builds else None,
            "build_key": next(iter(run.builds.values()), {}).get("build_key") if run.builds else None}


@router.get("/benchmarks/{tab}")
def benchmark_table(tab: str, normaliser: str | None = None, collar: float | None = None, metric: str = "wer",
                    skip_overlap: bool = False, session: Session = Depends(get_session)) -> dict[str, Any]:
    if tab not in ("asr", "diar"):
        raise HTTPException(404, "tab must be asr or diar (pipelines: /benchmarks-pipelines)")
    if metric not in ASR_METRICS:
        raise HTTPException(400, f"metric must be one of {', '.join(ASR_METRICS)}")
    skip_overlap = skip_overlap and tab == "diar"
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
    enh_ids = sorted({k[2] for k in runs if k[2] is not None})
    enhancers = [m for m in (session.get(Model, i) for i in enh_ids) if m is not None]
    saved: dict[int, list[Slice]] = {}
    for s in session.scalars(select(Slice).order_by(Slice.id)):
        saved.setdefault(s.dataset_id, []).append(s)
    cells = []
    for m in models:
        for d in datasets:
            plain: float | None = None
            for e in [None, *enhancers]:
                rs = runs.get((m.id, d.id, e.id if e else None, None), [])
                if e is not None and not rs:
                    continue
                cell = _cell(session, tab, m, d, rs, nkey, collar, skip_overlap, metric)
                cell["enh_model_id"] = e.id if e else None
                if saved.get(d.id) and cell.get("score_id"):
                    cell["slices"] = _slice_values(session, cell["score_id"], saved[d.id], tab, metric)
                if e is None:
                    plain = cell["value"]
                elif cell["value"] is not None and plain is not None:
                    cell["vs_original"] = cell["value"] - plain
                cells.append(cell)
    recent = list(session.scalars(select(Run).where(Run.task == run_task).order_by(Run.id.desc()).limit(8)))
    return {
        "tab": tab, "normaliser": nkey, "collar": collar, "metric": metric, "skip_overlap": skip_overlap,
        "datasets": [_ds_json(d, label in (d.label_types or []) and d.status == "ready") for d in datasets],
        "models": [{"id": m.id, "display_name": m.display_name, "catalog_id": m.catalog_id, "tags": m.tags,
                    "capabilities": m.capabilities, "timing_level": m.timing_level,
                    "kind": "asr" if tab == "asr" else ("diar" if m.has_speakers else "vad")} for m in models],
        "enhancers": [_enh_json(e) for e in enhancers],
        "copies": _copies(session),
        "slices": [{"id": s.id, "dataset_id": s.dataset_id, "name": s.name, "filters": s.filters}
                   for ss in saved.values() for s in ss],
        "cells": cells,
        "recent_runs": [{"id": r.id, "label": run_label(r), "dataset": r.dataset.name, "status": r.status,
                         "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in recent],
    }


def _slice_values(session: Session, score_id: int, saved: list[Slice], tab: str, metric: str) -> dict[str, Any]:
    """A score's value on each saved slice of its dataset: slice id -> {value, n}."""
    from ..analysis import slices as slices_an
    from .errors import _units

    sc = session.get(Score, score_id)
    us = _units(session, sc)
    m = metric if tab == "asr" else ("der" if (sc.aggregates or {}).get("der") is not None else "vad_error")
    out = {}
    for s in saved:
        sub = [u for u in us if slices_an.matches(u, s.filters)]
        out[str(s.id)] = {"value": slices_an.rate(sub, m) if sub else None, "n": len(sub)}
    return out


def _cell(session: Session, tab: str, m: Model, d: Dataset, rs: list[Run], nkey: str, collar: float,
          skip_overlap: bool, metric: str) -> dict[str, Any]:
    """One leaderboard cell: the latest done run's score, its change against the run before, and its compute."""
    done = [r for r in rs if r.status == "done"]
    latest = rs[-1] if rs else None
    cell: dict[str, Any] = {"model_id": m.id, "dataset_id": d.id, "status": latest.status if latest else None,
                            "run_id": None, "value": None, "prev": None, "delta": None}
    if latest and latest.status in ("failed", "blocked"):
        cell["error"] = latest.error
    if done:
        cur = done[-1]
        sc, job = ensure_score(session, cur, nkey, collar, skip_overlap=skip_overlap)
        cell["run_id"] = cur.id
        if job is not None:
            cell["pending_score_job"] = job.id
        if sc is not None:
            v, label_ = _value(tab, sc.aggregates, m.has_speakers, metric)
            cell.update({"value": v, "metric": label_, "score_id": sc.id,
                         "breakdown": _breakdown(tab, sc.aggregates, label_), **run_compute(cur)})
            if tab == "asr":
                cell["metrics"] = asr_metrics(sc.aggregates)
            if len(done) > 1:
                psc, _ = ensure_score(session, done[-2], nkey, collar, queue=False, skip_overlap=skip_overlap)
                if psc is not None:
                    pv, _ = _value(tab, psc.aggregates, m.has_speakers, metric)
                    cell["prev"] = pv
                    cell["delta"] = v - pv if v is not None and pv is not None else None
        if latest is not cur and latest.status in ("queued", "waiting", "running"):
            cell["rerunning"] = True
    return cell


def _pipe_values(session: Session, cur: Run, nkey: str, collar: float, skip_overlap: bool,
                 tkey: str) -> tuple[Score | None, Job | None, dict[str, Any]]:
    sc, job = ensure_score(session, cur, nkey, collar, skip_overlap=skip_overlap)
    if sc is None:
        return None, job, {}
    a = sc.aggregates
    return sc, job, {
        "score_id": sc.id, "wer": (a.get("wer") or {}).get("wer"),
        "cpwer": (a.get("cpwer") or {}).get("cpwer") if a.get("cpwer") else None,
        "tcpwer": ((a.get("tcpwer") or {}).get(tkey) or {}).get("tcpwer"),
        "der": (a.get("der") or {}).get("der") if a.get("der") else None,
        **run_compute(cur),
        "attribution": a.get("attribution"), "wrong_speaker_pairs": a.get("wrong_speaker_pairs"),
        "label": run_label(cur)}


@router.get("/benchmarks-pipelines")
def pipeline_grid(dataset_id: int | None = None, normaliser: str | None = None, collar: float | None = None,
                  skip_overlap: bool = False, tcp_collar: float = cpwer.DEFAULT_TCP_COLLAR,
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    """The pipeline grid on original audio (VAD / diarisation x ASR), and the leaderboard of every
    enhancement x VAD / diarisation x ASR combination run on the dataset, with its change against the same
    pipeline on original audio."""
    nkey = normalisers.get(normaliser or normalisers.DEFAULT).key
    collar = DEFAULT_COLLAR if collar is None else collar
    datasets = list(session.scalars(select(Dataset).order_by(Dataset.name)))
    eligible = [d for d in datasets if "asr" in (d.label_types or []) and d.status == "ready"]
    ds = session.get(Dataset, dataset_id) if dataset_id else (eligible[0] if eligible else None)
    models = list(session.scalars(select(Model).order_by(Model.display_name)))
    seg_models = [m for m in models if m.is_seg]
    asr_models = [m for m in models if m.is_asr]
    tkey = cpwer.tcp_key(tcp_collar)
    out: dict[str, Any] = {
        "normaliser": nkey, "collar": collar, "skip_overlap": skip_overlap, "tcp_collar": tcp_collar,
        "tcp_collars": list(cpwer.TCP_COLLARS), "dataset_id": ds.id if ds else None,
        "datasets": [_ds_json(d, d in eligible) for d in datasets],
        "rows": ([{"seg_model_id": None, "name": "Reference segments + speakers", "oracle": True,
                   "has_speakers": True, "available": bool(ds and "diar" in (ds.label_types or []))}] +
                 [{"seg_model_id": m.id, "name": m.display_name, "oracle": False, "has_speakers": m.has_speakers,
                   "available": True} for m in seg_models]),
        "cols": [{"asr_model_id": m.id, "name": m.display_name, "timing_level": m.timing_level} for m in asr_models],
        "enhancers": [_enh_json(m) for m in models if m.is_enh],
        "cells": [], "runs": [], "leaderboard": [], "copies": [],
    }
    if ds is None:
        return out
    pipes = {(p.seg_model_id, p.asr_model_id): p for p in session.scalars(select(Pipeline))}
    runs = _runs_by_key(session, RunTask.pipeline)
    for row in out["rows"]:
        for col in out["cols"]:
            p = pipes.get((row["seg_model_id"], col["asr_model_id"]))
            rs = runs.get((p.id, ds.id, None, None), []) if p else []
            done = [r for r in rs if r.status == "done"]
            cell: dict[str, Any] = {"seg_model_id": row["seg_model_id"], "asr_model_id": col["asr_model_id"],
                                    "pipeline_id": p.id if p else None, "status": rs[-1].status if rs else None,
                                    "run_id": None, "has_speakers": row["has_speakers"], "oracle": row["oracle"]}
            if rs and rs[-1].status in ("failed", "blocked"):
                cell["error"] = rs[-1].error
            if done:
                cur = done[-1]
                sc, job, vals = _pipe_values(session, cur, nkey, collar, skip_overlap, tkey)
                cell["run_id"] = cur.id
                if job is not None:
                    cell["pending_score_job"] = job.id
                cell.update(vals)
                if sc is not None and len(done) > 1:
                    psc, _ = ensure_score(session, done[-2], nkey, collar, queue=False, skip_overlap=skip_overlap)
                    if psc is not None and psc.aggregates.get("cpwer") and cell["cpwer"] is not None:
                        cell["prev_cpwer"] = psc.aggregates["cpwer"]["cpwer"]
            out["cells"].append(cell)
    ranked = [c for c in out["cells"] if c.get("score_id")]
    ranked.sort(key=lambda c: (c["cpwer"] is None, c["cpwer"] if c["cpwer"] is not None else c["wer"] or 9e9))
    out["runs"] = ranked
    # every combination run on this dataset, original audio included
    base = {(c["seg_model_id"], c["asr_model_id"]): c for c in ranked}
    by_pipe = {p.id: p for p in pipes.values()}
    lb, queued = [], 0
    for (pid, did, eid, place), rs in runs.items():
        if did != ds.id or pid not in by_pipe:
            continue
        p = by_pipe[pid]
        done = [r for r in rs if r.status == "done"]
        if not done:
            queued += any(r.status in ("queued", "waiting", "running", "blocked") for r in rs)
            continue
        cur = done[-1]
        _, _, vals = _pipe_values(session, cur, nkey, collar, skip_overlap, tkey)
        if not vals:
            continue
        b = base.get((p.seg_model_id, p.asr_model_id))
        entry = {"run_id": cur.id, "seg_model_id": p.seg_model_id, "asr_model_id": p.asr_model_id,
                 "oracle": p.seg_model_id is None, "has_speakers": p.seg_model_id is None or p.seg_model.has_speakers,
                 "enh_model_id": eid, "placement": place, "generative": bool(cur.enh_model and cur.enh_model.generative),
                 **vals, "rtf_total": (vals.get("rtf") or 0.0) + ((cur.rtf_by_stage or {}).get("enh") or 0.0)}
        if eid is not None and b is not None:
            key = "cpwer" if entry["cpwer"] is not None and b.get("cpwer") is not None else "wer"
            if entry.get(key) is not None and b.get(key) is not None:
                entry["vs_original"], entry["vs_metric"] = entry[key] - b[key], key
        lb.append(entry)
    lb.sort(key=lambda c: (c["cpwer"] is None, c["cpwer"] if c["cpwer"] is not None else c["wer"] or 9e9))
    out["leaderboard"] = lb
    out["queued"] = queued
    out["copies"] = [c for c in _copies(session) if c["dataset_id"] == ds.id]
    out["dataset"] = _ds_json(ds, ds in eligible)
    return out


# ---------------------------------------------------------------- plans

class PlanIn(BaseModel):
    dataset_id: int
    seg_models: list[str] = Field(default_factory=list)  # catalog model ids
    asr_models: list[str] = Field(default_factory=list)
    seg_model_ids: list[int] = Field(default_factory=list)  # Bench model ids (plans queued before the catalog)
    asr_model_ids: list[int] = Field(default_factory=list)
    oracle: bool = False
    mode: str = "grid"  # grid: every seg x every ASR as pipelines, stage by stage; stages: each model alone
    # Speech enhancement ablation (EVALUATE_PLAN.md §5.5): every model also runs on each enhancer's copy;
    # `include_original` keeps the original audio as the baseline; pipelines place the copy before and / or
    # after diarisation.
    enhancers: list[str] = Field(default_factory=list)
    include_original: bool = True
    placements: list[str] = Field(default_factory=lambda: ["before"])


def plan_size(seg: int, asr: int, oracle: bool, mode: str, enhancers: int = 0, include_original: bool = True,
              placements: int = 1) -> int:
    versions = int(include_original) + enhancers
    if mode == "grid":
        return seg * asr * (int(include_original) + enhancers * placements) + int(oracle) * asr * versions
    return (seg + asr) * versions


def _plan_models(session: Session, plan: dict[str, Any]) -> tuple[list[Model], list[Model]]:
    """The plan's models as rows (made on first use from the catalog), seg and ASR, capable ones only."""
    def pick(ids: list[int], cids: list[str]) -> list[Model]:
        out = [m for m in (session.get(Model, i) for i in ids) if m is not None]
        out += [catalog.ensure_model(session, c) for c in cids]
        return list(dict.fromkeys(out))

    seg = [m for m in pick(plan.get("seg_model_ids") or [], plan.get("seg_models") or []) if m.is_seg]
    asr = [m for m in pick(plan.get("asr_model_ids") or [], plan.get("asr_models") or []) if m.is_asr]
    return seg, asr


def _plan_enhancers(session: Session, plan: dict[str, Any]) -> list[Model]:
    return [m for m in dict.fromkeys(catalog.ensure_model(session, c) for c in plan.get("enhancers") or []) if m.is_enh]


def _placements(plan: dict[str, Any]) -> list[str]:
    places = [p for p in dict.fromkeys(plan.get("placements") or ["before"]) if p in PLACEMENTS]
    return places or ["before"]


def catalog_stages(session: Session, ds: Dataset, seg: list[Model], asr: list[Model], mode: str,
                   enhancers: list[Model] | None = None) -> list[dict]:
    """The plan request for the catalog (POST /api/jobs): enhancers whose copy is not built yet, then the seg
    stage with its index keys (whole files, so the catalog counts what is stored already), then the ASR
    stage, whose cuts are unknown until the seg stage has run. "Each stage alone" is two independent stages."""
    from .. import adapters
    from ..runs import enhance
    from ..runs.cache import WHOLE

    shas = [f.sha256 for f in executor.usable_files(session, ds.id)]
    stages = []
    to_build = [m for m in enhancers or [] if enhance.find(session, ds.id, m.id) is None]
    if to_build:
        stages.append({"name": "enh", "models": [m.catalog_id for m in to_build]})
    if seg:
        keys = {}
        for m in seg:
            try:
                ph = adapters.for_model(m).params_hash
            except adapters.AdapterError:
                continue
            keys[m.catalog_id] = [{"audio_sha256": sha, "span_start": WHOLE, "span_end": WHOLE, "params_hash": ph}
                                  for sha in shas]
        stages.append({"name": "seg", "models": [m.catalog_id for m in seg], "keys": keys,
                       **({"after": "enh"} if to_build else {})})
    if asr:
        stages.append({"name": "asr", "models": [m.catalog_id for m in asr],
                       **({"after": "seg"} if seg and mode == "grid" else {"after": "enh"} if to_build else {})})
    return stages


def _copy_status(session: Session, ds: Dataset, m: Model) -> dict[str, Any]:
    from ..runs import enhance

    c = enhance.find(session, ds.id, m.id, ("ready", "building"))
    return {"model": m.catalog_id, "display_name": m.display_name, "generative": m.generative,
            "status": c.status if c else "new", "progress": c.progress if c else 0.0, "copy_id": c.id if c else None}


def plan_preview(session: Session, ds: Dataset, plan: dict[str, Any]) -> dict[str, Any]:
    seg, asr = _plan_models(session, plan)
    enh = _plan_enhancers(session, plan)
    labels = set(ds.label_types or [])
    mode = plan.get("mode", "grid")
    oracle = bool(plan.get("oracle")) and "diar" in labels
    orig = bool(plan.get("include_original", True)) or not enh
    places = _placements(plan) if enh else ["before"]
    if mode == "grid":
        runs = plan_size(len(seg), len(asr), oracle, mode, len(enh), orig, len(places)) if "asr" in labels else 0
    else:
        seg, asr = (seg if "diar" in labels else []), (asr if "asr" in labels else [])
        runs = plan_size(len(seg), len(asr), False, mode, len(enh), orig)
    out: dict[str, Any] = {"runs": runs, "mode": mode, "plan": None, "error": None,
                           "copies": [_copy_status(session, ds, m) for m in enh]}
    if not runs or not (seg or asr):
        return out
    try:
        out["plan"] = catalog.plan(catalog_stages(session, ds, seg, asr, mode, enh))
    except catalog.CatalogUnavailable as exc:
        out["error"] = str(exc)
    return out


def enqueue_plan(session: Session, dataset_id: int, plan: dict[str, Any]) -> list[Job]:
    """Queue a plan: a grid as stage jobs plus one assemble job per pipeline, or each model as a single run,
    once per audio version (the original, and each enhancer's copy, before and / or after diarisation for
    pipelines). Stage jobs follow the catalog's order (models already running first, then by cold start)."""
    ds = session.get(Dataset, dataset_id)
    if ds is None or ds.status != "ready":
        return []
    seg, asr = _plan_models(session, plan)
    enh = _plan_enhancers(session, plan)
    orig = bool(plan.get("include_original", True)) or not enh
    oracle = bool(plan.get("oracle")) and "diar" in (ds.label_types or [])
    out: list[Job] = []
    if plan.get("mode", "grid") == "grid" and "asr" in (ds.label_types or []):
        seg_ids: list[int | None] = ([None] if oracle else []) + [m.id for m in seg]
        pipes = [get_or_create_pipeline(session, sid, a.id) for sid in seg_ids for a in asr]
        order = None
        try:
            steps = catalog.plan(catalog_stages(session, ds, seg, asr, "grid", enh)).get("steps") or []
            order = [st["model"] for st in steps]
        except catalog.CatalogUnavailable:
            pass
        versions: list[tuple[Model | None, str | None]] = ([(None, None)] if orig else []) + \
            [(e, place) for e in enh for place in _placements(plan)]
        for e, place in versions:
            for _, job in enqueue_pipelines(session, ds, pipes, order=order, enh_model=e, placement=place):
                out.append(job)
        dep_ids = {j for job in out for j in job.after}
        out += [j for j in (session.get(Job, i) for i in sorted(dep_ids)) if j is not None]
    else:
        for e in ([None] if orig else []) + enh:
            if "diar" in (ds.label_types or []):
                out += [enqueue_run(session, RunTask.seg, ds, model=m, enh_model=e)[1] for m in seg]
            if "asr" in (ds.label_types or []):
                out += [enqueue_run(session, RunTask.asr, ds, model=m, enh_model=e)[1] for m in asr]
        dep_ids = {j for job in out for j in job.after}
        out += [j for j in (session.get(Job, i) for i in sorted(dep_ids)) if j is not None]
    session.commit()
    return sorted(set(out), key=lambda j: j.id)


@router.post("/benchmarks/plan/preview")
def preview_plan(body: PlanIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """What a plan would do, before it is queued: runs, and the catalog's stages, model loads, index hits
    and estimated load time (`plan`; null with `error` when the catalog is unreachable)."""
    ds = session.get(Dataset, body.dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    try:
        out = plan_preview(session, ds, body.model_dump())
    except catalog.ModelUnusable as exc:
        raise HTTPException(404, str(exc)) from None
    except catalog.CatalogUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    session.commit()
    return out


@router.post("/benchmarks/plan", status_code=202)
def run_plan(body: PlanIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = session.get(Dataset, body.dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    if ds.status != "ready":
        raise HTTPException(409, f"dataset is {ds.status}")
    try:
        js = enqueue_plan(session, ds.id, body.model_dump())
    except catalog.ModelUnusable as exc:
        raise HTTPException(404, str(exc)) from None
    except catalog.CatalogUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    runs = sum(1 for j in js if j.type == "run")
    return {"queued": runs, "jobs": [job_brief(j) for j in js]}


@router.get("/suggestions/models")
def suggest_models(dataset_id: int | None = None, tags: str = "", language: str | None = None,
                   label_types: str = "", session: Session = Depends(get_session)) -> dict[str, Any]:
    """Catalog models grouped by stage, ranked by tag overlap with the dataset (or with the tags being typed),
    each with its state, capabilities and cold start."""
    if dataset_id:
        ds = session.get(Dataset, dataset_id)
        if ds is None:
            raise HTTPException(404, "dataset not found")
        tag_list, language, labels = ds.tags, ds.language, set(ds.label_types or [])
    else:
        tag_list, labels = [t for t in tags.split(",") if t], {t for t in label_types.split(",") if t}
    listing = catalog_listing(session)
    entries = [e for e in listing["models"] if e["in_catalog"]]
    by_id = {e["catalog_id"]: e for e in entries}
    ranked = rank_models(tag_list, entries, language)

    def entry(r: dict[str, Any]) -> dict[str, Any]:
        return {**r, **by_id[r["catalog_id"]]}

    return {
        "seg": [entry(r) for r in ranked if "seg" in r["stages"]],
        "asr": [entry(r) for r in ranked if "asr" in r["stages"]],
        "enh": [entry(r) for r in ranked if "enh" in r["stages"]],
        "oracle_available": "diar" in labels,
        "labels": sorted(labels),
        "catalog": listing["catalog"],
    }


@router.get("/scores/{score_id}")
def score_detail(score_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    sc = session.get(Score, score_id)
    if sc is None:
        raise HTTPException(404, "score not found")
    return {"id": sc.id, "run_id": sc.run_id, "normaliser": sc.normaliser, "collar": sc.collar,
            "aggregates": sc.aggregates, "label": run_label(sc.run), "dataset_id": sc.run.dataset_id}
