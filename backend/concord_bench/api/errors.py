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
from ..analysis import slices as slices_an
from ..core.db import get_session
from ..core.models import AudioFile, Result, Run, RunTask, Score, Segment
from ..datasets.labels import of as labels_of
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
    names = {f.id: f.stem for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == run.dataset_id))}
    out = {"run": run_json(run), "score_id": sc.id, "collar": sc.collar, "skip_overlap": sc.skip_overlap,
           "has_speakers": run.model.has_speakers,
           "tiles": _diar_tiles(sc.aggregates), "compare": None,
           "conditions": diar_an.by_condition(results, _meta_by_file(session, run.dataset_id)),
           "turns_found": diar_an.turns_found(results), "boundaries": diar_an.boundary_histogram(results),
           "regions": diar_an.regions(results, names), "speaker_counts": diar_an.speaker_counts(results)}
    if compare_run_id:
        cr = get_run(session, compare_run_id)
        csc = _score_for(session, cr, None, collar, skip_overlap)
        cres = list(session.scalars(select(Result).where(Result.score_id == csc.id)))
        out["compare"] = {"run": run_json(cr), "score_id": csc.id, "tiles": _diar_tiles(csc.aggregates),
                          "regions": diar_an.regions(cres, names)}
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
    masks = diar_an._moments(al.get("ref") or [], r.end or 0.0)
    errors = [{**e, "context": diar_an.context_of(e["start"], e["end"], masks)} for e in al.get("errors", [])]
    f = session.get(AudioFile, r.audio_file_id)
    spk_labels: dict[str, dict[str, Any]] = {}
    for s in session.scalars(select(Segment).where(Segment.audio_file_id == r.audio_file_id, Segment.speaker.is_not(None))):
        for k, v in labels_of(s.meta).items():
            spk_labels.setdefault(s.speaker, {})[k] = v
    return {**_file_json(r), "ref": al.get("ref", []),
            "hyp": [[s, e, mapping.get(k, k), k] for s, e, k in al.get("hyp", [])], "mapping": mapping,
            "errors": errors, "overlaps": al.get("overlaps", []),
            "largest": sorted(errors, key=lambda e: -e["dur"])[:6], "confusion": diar_an.confusion(r),
            "speaker_labels": spk_labels, "file_labels": labels_of(f.meta if f else {}),
            "label_url": peer.file_link_for(session, r.audio_file_id)}


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
    results = list(session.scalars(select(Result).where(Result.score_id == sc.id)))
    return {"run": run_json(run), "score_id": sc.id, "normaliser": sc.normaliser, "collar": sc.collar,
            "skip_overlap": sc.skip_overlap, "tcp_collar": tcp_collar, "tcp_collars": list(cpwer.TCP_COLLARS),
            "stages": _stages(run, sc, results), "waterfall": _waterfall(tiles),
            "moments": _moments(sc.aggregates.get("moments")),
            "tiles": tiles, "counterfactuals": _counterfactuals(tiles["attribution"]),
            "wrong_speaker_pairs": sc.aggregates.get("wrong_speaker_pairs", []),
            "has_speakers": sc.aggregates.get("has_speakers"), "oracle": p.seg_model_id is None,
            "chain": [{"stage": "VAD / diarisation", "name": p.seg_model.display_name if p.seg_model else
                       "Reference segments + speakers"}, {"stage": "ASR", "name": p.asr_model.display_name}]}


def _stages(run: Run, sc: Score, results: list[Result]) -> list[dict[str, Any]]:
    """The pipeline's stage cards: each stage's model, metric, hardware, RTF and what it hands on."""
    agg, rtf, hw = sc.aggregates or {}, run.rtf_by_stage or {}, run.hardware or {}
    p = run.pipeline
    segs = [len(((r.alignment or {}).get("seg") or {}).get("hyp") or []) for r in results]
    spks = [len({h[2] for h in ((r.alignment or {}).get("seg") or {}).get("hyp") or []}) for r in results]
    cuts = sum(len((r.alignment or {}).get("hyp_segments") or []) for r in results)
    out = []
    if run.enh_model is not None:
        out.append({"kind": "enhance", "name": run.enh_model.display_name, "metric": None, "metric_label": None,
                    "rtf": rtf.get("enh"), "hardware": hw.get(run.enh_model.catalog_id) or {},
                    "placement": run.enh_placement, "generative": run.enh_model.generative,
                    "handoff": "enhanced audio, same length", "copy_id": run.enh_copy_id})
    der = (agg.get("der") or {}).get("der") if agg.get("der") else None
    out.append({"kind": "seg", "name": p.seg_model.display_name if p.seg_model else "Reference segments + speakers",
                "oracle": p.seg_model is None, "metric": der, "metric_label": "DER",
                "rtf": rtf.get("seg"), "hardware": hw.get(p.seg_model.catalog_id) or {} if p.seg_model else {},
                "hears": "enhanced" if run.enh_model is not None and run.enh_placement == "before" else "original",
                "handoff": f"{sum(segs)} segments, {max(spks) if spks else 0} speakers at most per file"})
    out.append({"kind": "asr", "name": p.asr_model.display_name, "metric": (agg.get("wer") or {}).get("wer"),
                "metric_label": "WER", "rtf": rtf.get("asr"), "hardware": hw.get(p.asr_model.catalog_id) or {},
                "hears": "enhanced" if run.enh_model is not None else "original",
                "handoff": f"{cuts} transcribed segments"})
    cp = (agg.get("cpwer") or {}).get("cpwer") if agg.get("cpwer") else None
    out.append({"kind": "output", "name": "Speaker-attributed transcript", "metric": cp, "metric_label": "cpWER",
                "rtf": rtf.get("total"), "wall_s": run.wall_s})
    return out


def _waterfall(tiles: dict[str, Any]) -> list[dict[str, Any]] | None:
    """ASR alone, then segmentation, speaker attribution (to cpWER) and timing (to tcpWER), as rates."""
    att = tiles.get("attribution")
    if not att or not att.get("n"):
        return None
    n = att["n"]
    steps = [{"key": "asr", "label": "ASR alone", "sub": "Reference segments + speakers", "value": att["asr"],
              "total": att["asr"]},
             {"key": "segmentation", "label": "Segmentation", "sub": "model segments, reference speakers",
              "value": att["segmentation"], "total": att["b_errors"] / n},
             {"key": "speaker", "label": "Speaker attribution", "sub": "model speakers", "value": att["speaker"],
              "total": att["c_errors"] / n}]
    if tiles.get("tcp_errors") is not None:
        timing = (tiles["tcp_errors"] - att["c_errors"]) / n
        steps.append({"key": "timing", "label": "Timing", "sub": "words outside the tcpWER collar", "value": timing,
                      "total": tiles["tcp_errors"] / n})
    return steps


def _moments(m: dict[str, Any] | None) -> list[dict[str, Any]]:
    from ..runs.scoring import MOMENTS

    out = []
    for k, label in MOMENTS.items():
        v = (m or {}).get(k)
        if not v or not v.get("words"):
            continue
        w = v["words"]
        parts = {c: v[c] / w for c in ("asr", "segmentation", "speaker")}
        main = max(parts, key=lambda c: parts[c]) if any(parts.values()) else None
        out.append({"key": k, "label": label, "words": w, "parts": parts, "rate": sum(parts.values()), "main": main})
    return out


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
            "ref_lines": al.get("ref_lines") or [], "moments": (r.metrics or {}).get("moments"),
            "tcp": (r.metrics or {}).get("tcp"),
            "label_url": peer.file_link_for(session, r.audio_file_id)}


# ---------------------------------------------------------------- slices by label

def _units(session: Session, sc: Score) -> list[slices_an.Unit]:
    results = list(session.scalars(select(Result).where(Result.score_id == sc.id)))
    files = {f.id: f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == sc.run.dataset_id))}
    seg_ids = [r.segment_id for r in results if r.segment_id]
    segs = {s.id: s for s in session.scalars(select(Segment).where(Segment.id.in_(seg_ids)))} if seg_ids else {}
    return slices_an.units(results, sc.run.task, files, segs)


def _score(session: Session, score_id: int) -> Score:
    sc = session.get(Score, score_id)
    if sc is None:
        raise HTTPException(404, "score not found")
    return sc


def _metric_for(sc: Score, metric: str | None) -> str:
    m = metric or slices_an.DEFAULT_METRIC[sc.run.task]
    if m not in slices_an.METRICS:
        raise HTTPException(400, f"metric must be one of {', '.join(slices_an.METRICS)}")
    if sc.run.task == RunTask.seg and m not in ("der", "vad_error"):
        raise HTTPException(400, "VAD / diarisation slices take der or vad_error")
    if sc.run.task == RunTask.pipeline and m not in ("cpwer", "wer", "cer"):
        raise HTTPException(400, "pipeline slices take cpwer, wer or cer")
    return m


@router.get("/slices")
def slices(score_id: int, compare_score_id: int | None = None, metric: str | None = None, label: str | None = None,
           bins: str = "even", edges: str | None = None, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Scores by label for one score: the labels with how many weak values each has, then either the slices of
    one label (`label`) or the weakest slices of every label (the "where it fails" table)."""
    sc = _score(session, score_id)
    m = _metric_for(sc, metric)
    if sc.run.task == RunTask.seg and m == "der" and not (sc.aggregates or {}).get("der"):
        m = "vad_error"
    us = _units(session, sc)
    other = _units(session, _score(session, compare_score_id)) if compare_score_id else None
    custom = [float(x) for x in edges.split(",") if x.strip()] if edges else None
    infos = slices_an.label_info(us)
    for info in infos:
        if info["kind"] != "unused":
            rows = slices_an.label_rows(us, info["key"], m, sc.run.task, other)
            info["weak"] = sum(1 for r in rows if r["pattern"] in ("worse", "hard"))
            info["values"] = len(rows)
    out: dict[str, Any] = {"score_id": sc.id, "task": sc.run.task, "metric": m, "low_better": slices_an.METRICS[m][2],
                           "overall": {"n": len(us), "value": slices_an.rate(us, m), "ci": slices_an.interval(us, m)},
                           "labels": infos, "min_units": slices_an.MIN_UNITS[sc.run.task]}
    if other is not None:
        out["compare_overall"] = {"n": len(other), "value": slices_an.rate(other, m)}
    if label:
        out["label"] = label
        out["rows"] = slices_an.label_rows(us, label, m, sc.run.task, other, bins, custom)
        have = [u for u in us if label in u.labels]
        out["unlabelled"] = len(us) - len(have)
    else:
        out["finder"] = slices_an.finder(us, m, sc.run.task, other)
    return out


@router.get("/slices/heatmap")
def slices_heatmap(score_id: int, row: str, col: str, metric: str | None = None,
                   session: Session = Depends(get_session)) -> dict[str, Any]:
    sc = _score(session, score_id)
    m = _metric_for(sc, metric)
    return {"metric": m, "min_units": slices_an.HEAT_MIN, **slices_an.heatmap(_units(session, sc), row, col, m)}


@router.get("/slices/units")
def slice_units(score_id: int, filters: str, metric: str | None = None,
                session: Session = Depends(get_session)) -> dict[str, Any]:
    """One slice (JSON filters, as saved slices hold them): its score and interval, and its worst units."""
    import json

    sc = _score(session, score_id)
    m = _metric_for(sc, metric)
    try:
        fl = json.loads(filters)
    except ValueError:
        raise HTTPException(400, "filters must be JSON") from None
    us = _units(session, sc)
    sub = [u for u in us if slices_an.matches(u, fl)]
    sub_counts = {k: sum(u.counts.get(k, 0) for u in sub) for k in ("n", "s", "d", "i", "missed", "false_alarm",
                                                                    "confusion", "total", "cp_errors", "cp_n")}
    return {"metric": m, "n": len(sub), "of": len(us), "value": slices_an.rate(sub, m) if sub else None,
            "ci": slices_an.interval(sub, m), "overall": slices_an.rate(us, m), "counts": sub_counts,
            "worst": slices_an.worst(sub), "unit_ids": [u.id for u in sub][:5000]}


@router.get("/runs")
def runs_for_errors(task: str, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Done runs for the page pickers, newest first."""
    runs = session.scalars(select(Run).where(Run.task == task, Run.status == "done").order_by(Run.id.desc()))
    return [run_json(r) for r in runs]


@router.get("/normalisers")
def normaliser_keys() -> list[str]:
    return [n.key for n in normalisers.BUILTIN.values()]
