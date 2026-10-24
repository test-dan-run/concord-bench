"""Error analysis: ASR utterances, VAD / diarisation files, pipeline files.

Pages are addressed by run (plus normaliser / collar). Tables are sorted, filtered and searched
server-side over `result` rows.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import Float, and_, func, or_, select
from sqlalchemy.orm import Session

from .. import peer
from ..analysis import asr as asr_an
from ..analysis import diar as diar_an
from ..core.db import get_session
from ..core.models import AudioFile, Result, Run, RunTask, Score, Segment
from ..metrics import attribution, cpwer, normalisers, wer
from .runs import ensure_score, get_run, run_json, run_label

router = APIRouter(prefix="/errors", tags=["errors"])


def _score_for(session: Session, run: Run, normaliser: str | None, collar: float | None,
               skip_overlap: bool = False) -> Score:
    if run.status != "done":
        raise HTTPException(409, f"run is {run.status}")
    try:
        sc, job = ensure_score(session, run, normaliser, collar, skip_overlap=skip_overlap)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from None
    if sc is None:
        raise HTTPException(409, f"scoring with {normaliser} is queued (job {job.id if job else '?'}); try again "
                            "when it finishes")
    return sc


def _meta_by_file(session: Session, dataset_id: int) -> dict[int, dict[str, Any]]:
    return {f.id: f.meta or {} for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == dataset_id))}


SORTS = {
    "key": Result.utterance_key, "start": Result.start, "wer": Result.wer, "errors": Result.errors,
    "duration": Result.end - Result.start, "speaker": Result.speaker,
}


def _json_sort(field: str):
    return func.coalesce(func.json_extract(Result.metrics, f"$.{field}"), -1).cast(Float)


def _page(session: Session, stmt, sort: str, order: str, offset: int, limit: int, json_fields: tuple[str, ...]):
    col = SORTS.get(sort) if sort in SORTS else (_json_sort(sort) if sort in json_fields else Result.utterance_key)
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    col = col.desc() if order == "desc" else col.asc()
    rows = session.scalars(stmt.order_by(col.nulls_last(), Result.utterance_key).offset(offset).limit(limit)).all()
    return total, rows


# ---------------------------------------------------------------- ASR

def _asr_tiles(agg: dict[str, Any]) -> dict[str, Any]:
    a = wer.with_info(agg)
    return {k: a.get(k) for k in ("wer", "cer", "mer", "wil", "wip", "s", "d", "i", "h", "n", "p", "errors",
                                  "utterances", "with_errors")}


@router.get("/asr/summary")
def asr_summary(run_id: int, normaliser: str | None = None, compare_run_id: int | None = None,
                session: Session = Depends(get_session)) -> dict[str, Any]:
    run = get_run(session, run_id)
    if run.task != RunTask.asr:
        raise HTTPException(400, "not an ASR run")
    sc = _score_for(session, run, normaliser, None)
    out: dict[str, Any] = {"run": run_json(run), "score_id": sc.id, "normaliser": sc.normaliser,
                           "tiles": _asr_tiles(sc.aggregates), "timing_level": run.model.timing_level,
                           "compare": None}
    if compare_run_id:
        cr = get_run(session, compare_run_id)
        csc = _score_for(session, cr, normaliser, None)
        out["compare"] = {"run": run_json(cr), "score_id": csc.id, "tiles": _asr_tiles(csc.aggregates)}
    results = list(session.scalars(select(Result).where(Result.score_id == sc.id)))
    out["words"] = asr_an.word_errors(results)
    out["pairs"] = asr_an.substitution_pairs(results)
    out["slices"] = asr_an.slices(results, _meta_by_file(session, run.dataset_id))
    return out


def _utt_json(r: Result, flagged: bool | None = None) -> dict[str, Any]:
    m = wer.with_info(r.metrics or {})
    return {"id": r.id, "key": r.utterance_key, "file_id": r.audio_file_id, "segment_id": r.segment_id,
            "start": r.start, "end": r.end, "duration": round(r.end - r.start, 3), "speaker": r.speaker,
            "ref": r.ref_text, "hyp": r.hyp_text, "ops": (r.alignment or {}).get("ops", []),
            "n": m.get("n"), "s": m.get("s"), "d": m.get("d"), "i": m.get("i"), "errors": m.get("errors"),
            "wer": m.get("wer"), "cer": m.get("cer"), "mer": m.get("mer"), "wil": m.get("wil"), "wip": m.get("wip"),
            "flagged": flagged}


@router.get("/asr/utterances")
def asr_utterances(score_id: int, sort: str = "wer", order: str = "desc", q: str | None = None,
                   only_errors: bool = False, word: str | None = None, offset: int = 0,
                   limit: int = Query(100, le=1_000_000), session: Session = Depends(get_session)) -> dict[str, Any]:
    stmt = select(Result).where(Result.score_id == score_id)
    if only_errors:
        stmt = stmt.where(Result.errors > 0)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Result.ref_text.ilike(like), Result.hyp_text.ilike(like),
                              Result.utterance_key.ilike(like)))
    if word:
        ids = [r.id for r in session.scalars(stmt) if asr_an.has_error_word(r, word)]
        stmt = select(Result).where(Result.id.in_(ids))
    total, rows = _page(session, stmt, sort, order, offset, limit, ("n", "s", "d", "i", "cer", "mer", "wil", "wip"))
    seg_ids = [r.segment_id for r in rows if r.segment_id]
    flags = dict(session.execute(select(Segment.id, Segment.flagged).where(Segment.id.in_(seg_ids))).all()) \
        if seg_ids else {}
    return {"total": total, "offset": offset, "rows": [_utt_json(r, flags.get(r.segment_id)) for r in rows]}


@router.get("/asr/utterances.csv")
def asr_utterances_csv(score_id: int, q: str | None = None, only_errors: bool = False, word: str | None = None,
                       session: Session = Depends(get_session)) -> Response:
    """Every utterance matching the table's filters, as CSV."""
    page = asr_utterances(score_id, "key", "asc", q, only_errors, word, 0, 1_000_000, session)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["utterance", "speaker", "start", "end", "reference", "hypothesis", "n", "s", "d", "i", "wer", "cer"])
    for r in page["rows"]:
        w.writerow([r["key"], r["speaker"] or "", r["start"], r["end"], r["ref"], r["hyp"], r["n"], r["s"], r["d"],
                    r["i"], "" if r["wer"] is None else round(r["wer"], 4), "" if r["cer"] is None else round(r["cer"], 4)])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="utterances-score{score_id}.csv"'})


@router.get("/asr/utterances/{result_id}")
def asr_utterance(result_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    r = session.get(Result, result_id)
    if r is None:
        raise HTTPException(404, "utterance not found")
    sc = session.get(Score, r.score_id)
    seg = session.get(Segment, r.segment_id) if r.segment_id else None
    d = _utt_json(r, seg.flagged if seg else None)
    d["timed"] = bool((r.alignment or {}).get("timed"))
    d["segments"] = (r.alignment or {}).get("segments")
    d["flag_note"] = seg.flag_note if seg else None
    d["label_url"] = peer.file_link_for(session, r.audio_file_id, r.start)
    # Other models on the same utterance: latest done ASR run per model on this dataset, same normaliser.
    others = []
    runs = session.scalars(select(Run).where(Run.task == RunTask.asr, Run.dataset_id == sc.run.dataset_id,
                                             Run.status == "done", Run.id != sc.run_id).order_by(Run.id)).all()
    latest: dict[int, Run] = {}
    for run in runs:
        if run.model_id != sc.run.model_id:
            latest[run.model_id] = run
    for run in latest.values():
        osc = session.scalar(select(Score).where(Score.run_id == run.id, Score.normaliser == sc.normaliser))
        if osc is None:
            continue
        o = session.scalar(select(Result).where(Result.score_id == osc.id, Result.utterance_key == r.utterance_key))
        if o is not None:
            others.append({"run_id": run.id, "model": run_label(run), "hyp": o.hyp_text, "wer": o.wer,
                           "ops": (o.alignment or {}).get("ops", [])})
    d["others"] = sorted(others, key=lambda o: (o["wer"] is None, o["wer"]))
    return d


# ---------------------------------------------------------------- VAD / diarisation

def _diar_tiles(agg: dict[str, Any]) -> dict[str, Any]:
    tot = agg.get("total") or 0
    vt = agg.get("vad_total") or 0
    return {"der": agg.get("der"), "jer": agg.get("jer"), "speaker_count_acc": agg.get("speaker_count_acc"),
            "missed": (agg.get("missed") or 0) / tot if tot else None,
            "false_alarm": (agg.get("false_alarm") or 0) / tot if tot else None,
            "confusion": (agg.get("confusion") or 0) / tot if tot else None,
            "vad_error": agg.get("vad_error"), "vad_missed": (agg.get("vad_missed") or 0) / vt if vt else None,
            "vad_false_alarm": (agg.get("vad_false_alarm") or 0) / vt if vt else None, "files": agg.get("files")}


@router.get("/diar/summary")
def diar_summary(run_id: int, collar: float | None = None, compare_run_id: int | None = None,
                 skip_overlap: bool = False, session: Session = Depends(get_session)) -> dict[str, Any]:
    run = get_run(session, run_id)
    if run.task != RunTask.seg:
        raise HTTPException(400, "not a VAD / diarisation run")
    sc = _score_for(session, run, None, collar, skip_overlap)
    results = list(session.scalars(select(Result).where(Result.score_id == sc.id)))
    out = {"run": run_json(run), "score_id": sc.id, "collar": sc.collar, "skip_overlap": sc.skip_overlap,
           "has_speakers": run.model.has_speakers,
           "tiles": _diar_tiles(sc.aggregates), "compare": None,
           "conditions": diar_an.by_condition(results, _meta_by_file(session, run.dataset_id)),
           "turns_found": diar_an.turns_found(results), "boundaries": diar_an.boundary_histogram(results)}
    if compare_run_id:
        cr = get_run(session, compare_run_id)
        csc = _score_for(session, cr, None, collar, skip_overlap)
        out["compare"] = {"run": run_json(cr), "tiles": _diar_tiles(csc.aggregates)}
    return out


def _file_json(r: Result) -> dict[str, Any]:
    m = r.metrics or {}
    keys = ("duration", "der", "missed", "false_alarm", "confusion", "total", "jer", "ref_speakers", "hyp_speakers",
            "vad_error", "vad_missed", "vad_false_alarm", "vad_total", "ref_turns", "hyp_turns", "wer", "cpwer",
            "tcpwer", "rtf")
    return {"id": r.id, "key": r.utterance_key, "file_id": r.audio_file_id, **{k: m.get(k) for k in keys}}


@router.get("/diar/files")
def diar_files(score_id: int, sort: str = "der", order: str = "desc", q: str | None = None, offset: int = 0,
               limit: int = Query(100, le=1000), session: Session = Depends(get_session)) -> dict[str, Any]:
    stmt = select(Result).where(Result.score_id == score_id)
    if q:
        stmt = stmt.where(Result.utterance_key.ilike(f"%{q}%"))
    sort = "wer" if sort == "der" else sort  # result.wer holds DER (or VAD error) for seg runs
    total, rows = _page(session, stmt, sort, order, offset, limit,
                        ("missed", "false_alarm", "confusion", "jer", "vad_error", "hyp_speakers", "ref_speakers"))
    return {"total": total, "offset": offset, "rows": [_file_json(r) for r in rows]}


@router.get("/diar/files/{result_id}")
def diar_file(result_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    r = session.get(Result, result_id)
    if r is None:
        raise HTTPException(404, "file not found")
    al = r.alignment or {}
    mapping = al.get("mapping") or {}
    return {**_file_json(r), "ref": al.get("ref", []),
            "hyp": [[s, e, mapping.get(k, k), k] for s, e, k in al.get("hyp", [])], "mapping": mapping,
            "errors": al.get("errors", []), "overlaps": al.get("overlaps", []),
            "largest": diar_an.largest_errors(al), "label_url": peer.file_link_for(session, r.audio_file_id)}


# ---------------------------------------------------------------- pipelines

def _pipe_tiles(agg: dict[str, Any], tcp_collar: float = cpwer.DEFAULT_TCP_COLLAR) -> dict[str, Any]:
    att = agg.get("attribution")
    tcp = ((agg.get("tcpwer") or {}).get(cpwer.tcp_key(tcp_collar)) or {})
    return {"cpwer": (agg.get("cpwer") or {}).get("cpwer") if agg.get("cpwer") else None,
            "tcpwer": tcp.get("tcpwer"), "tcp_errors": tcp.get("errors"),
            "wer": (agg.get("wer") or {}).get("wer"), "der": (agg.get("der") or {}).get("der") if agg.get("der")
            else None, "attribution": att, "attribution_chart": attribution.clipped(att) if att else None,
            "cp_errors": (agg.get("cpwer") or {}).get("errors") if agg.get("cpwer") else None,
            "n": (agg.get("cpwer") or agg.get("wer") or {}).get("n")}


def _counterfactuals(att: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not att or not att.get("n"):
        return []
    n = att["n"]
    return [
        {"stage": "asr", "label": "If ASR were perfect", "cpwer": (att["c_errors"] - att["a_errors"]) / n,
         "note": "only segmentation and speaker errors left"},
        {"stage": "segmentation", "label": "If segmentation were perfect (reference segments)",
         "cpwer": att["a_errors"] / n, "note": "the “with reference segments” score"},
        {"stage": "speaker", "label": "If speaker attribution were perfect", "cpwer": att["b_errors"] / n,
         "note": "model segments, reference speakers"},
    ]


@router.get("/pipeline/summary")
def pipeline_summary(run_id: int, normaliser: str | None = None, collar: float | None = None,
                     tcp_collar: float = cpwer.DEFAULT_TCP_COLLAR, skip_overlap: bool = False,
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    run = get_run(session, run_id)
    if run.task != RunTask.pipeline:
        raise HTTPException(400, "not a pipeline run")
    sc = _score_for(session, run, normaliser, collar, skip_overlap)
    tiles = _pipe_tiles(sc.aggregates, tcp_collar)
    p = run.pipeline
    return {"run": run_json(run), "score_id": sc.id, "normaliser": sc.normaliser, "collar": sc.collar,
            "skip_overlap": sc.skip_overlap, "tcp_collar": tcp_collar, "tcp_collars": list(cpwer.TCP_COLLARS),
            "tiles": tiles, "counterfactuals": _counterfactuals(tiles["attribution"]),
            "wrong_speaker_pairs": sc.aggregates.get("wrong_speaker_pairs", []),
            "has_speakers": sc.aggregates.get("has_speakers"), "oracle": p.seg_model_id is None,
            "chain": [{"stage": "VAD / diarisation", "name": p.seg_model.display_name if p.seg_model else
                       "Reference segments + speakers"}, {"stage": "ASR", "name": p.asr_model.display_name}]}


@router.get("/pipeline/files")
def pipeline_files(score_id: int, sort: str = "cpwer", order: str = "desc", q: str | None = None, offset: int = 0,
                   limit: int = Query(100, le=1000), session: Session = Depends(get_session)) -> dict[str, Any]:
    stmt = select(Result).where(Result.score_id == score_id)
    if q:
        stmt = stmt.where(or_(Result.utterance_key.ilike(f"%{q}%"), Result.hyp_text.ilike(f"%{q}%"),
                              Result.ref_text.ilike(f"%{q}%")))
    sort = "wer" if sort == "cpwer" else sort  # result.wer holds cpWER when available, else WER
    total, rows = _page(session, stmt, sort, order, offset, limit, ("der", "rtf"))
    out = []
    for r in rows:
        d = _file_json(r)
        d["wer"] = (r.metrics or {}).get("wer")
        d["attribution"] = (r.metrics or {}).get("attribution")
        out.append(d)
    return {"total": total, "offset": offset, "rows": out}


@router.get("/pipeline/files/{result_id}")
def pipeline_file(result_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    r = session.get(Result, result_id)
    if r is None:
        raise HTTPException(404, "file not found")
    al = r.alignment or {}
    seg = al.get("seg") or {}
    mapping = seg.get("mapping") or {}
    ref_segs = session.scalars(select(Segment).where(and_(Segment.audio_file_id == r.audio_file_id,
                                                          Segment.speaker.is_not(None), Segment.text.is_not(None)))
                               .order_by(Segment.start)).all()
    cp = al.get("cpwer") or {}
    return {**_file_json(r), "wer": (r.metrics or {}).get("wer"), "attribution": (r.metrics or {}).get("attribution"),
            "ref": seg.get("ref", []), "hyp": [[s, e, mapping.get(k, k), k] for s, e, k in seg.get("hyp", [])],
            "errors": seg.get("errors", []), "overlaps": seg.get("overlaps", []), "mapping": mapping,
            "ref_segments": [{"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text} for s in ref_segs],
            "hyp_segments": al.get("hyp_segments", []), "assignment": cp.get("assignment", []),
            "pairs": cp.get("pairs", []), "wrong_speaker_words": cp.get("wrong_speaker_words", []),
            "ref_norm": al.get("ref_norm"), "hyp_norm": al.get("hyp_norm"),
            "label_url": peer.file_link_for(session, r.audio_file_id)}


@router.get("/runs")
def runs_for_errors(task: str, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Done runs for the page pickers, newest first."""
    runs = session.scalars(select(Run).where(Run.task == task, Run.status == "done").order_by(Run.id.desc()))
    return [run_json(r) for r in runs]


@router.get("/normalisers")
def normaliser_keys() -> list[str]:
    return [n.key for n in normalisers.BUILTIN.values()]
