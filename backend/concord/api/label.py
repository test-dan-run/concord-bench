"""Label (Phase 2): review console, ensemble pseudo-labelling, annotation, live evaluation, promotion."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_session
from ..core.models import AudioFile, Job, Model, Run, RunTask, Segment, SegmentStatus
from ..label import console, edit, fusion, live, promote, pseudo
from ..runs import jobs
from .datasets import _clean_tags, dataset_json, get_dataset, job_brief
from .runs import ACTIVE, run_json

router = APIRouter(prefix="/label", tags=["label"])


@router.get("/console")
def review_console(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return console.console(session)


# ---------------------------------------------------------------- pseudo-labelling

def _models(session: Session, seg_model_id: int | None, asr_model_ids: list[int]) -> tuple[Model, list[Model]]:
    seg = session.get(Model, seg_model_id or -1)
    if seg is None or not seg.is_seg:
        raise HTTPException(422, "pick a VAD / diarisation model for the segments")
    asr = []
    for i in asr_model_ids:
        m = session.get(Model, i)
        if m is None or not m.is_asr:
            raise HTTPException(422, f"model {i} is not a registered ASR model")
        asr.append(m)
    return seg, asr


def _ids(csv: str) -> list[int]:
    try:
        return [int(x) for x in csv.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "asr_model_ids must be comma-separated ids") from None


@router.get("/datasets/{dataset_id}/preview")
def label_preview(dataset_id: int, seg_model_id: int, asr_model_ids: str = "",
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    """Cost of a label run: which members' outputs are already saved for this dataset."""
    ds = get_dataset(session, dataset_id)
    seg, asr = _models(session, seg_model_id, _ids(asr_model_ids))
    return pseudo.preview(session, ds, seg, asr)


class LabelRunIn(BaseModel):
    tasks: list[str] = Field(default_factory=lambda: ["asr", "diar"])
    seg_model_id: int
    asr_model_ids: list[int] = Field(default_factory=list)
    strategy: str = "best_aligned"
    single_model_id: int | None = None
    high: float = Field(default=fusion.DEFAULT_HIGH, ge=0, le=1)
    low: float = Field(default=fusion.DEFAULT_LOW, ge=0, le=1)


@router.post("/datasets/{dataset_id}/runs", status_code=202)
def start_label_run(dataset_id: int, body: LabelRunIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = get_dataset(session, dataset_id)
    if ds.status != "ready":
        raise HTTPException(409, f"dataset {ds.name!r} is {ds.status}")
    tasks = [t for t in body.tasks if t in ("asr", "diar")]
    if not tasks:
        raise HTTPException(422, "choose at least one task: asr (transcripts) or diar (speakers)")
    if body.strategy not in fusion.STRATEGIES:
        raise HTTPException(422, f"strategy must be one of {', '.join(fusion.STRATEGIES)}")
    if body.low > body.high:
        raise HTTPException(422, "the review threshold must not be above the auto-accept threshold")
    seg, asr = _models(session, body.seg_model_id, body.asr_model_ids if "asr" in tasks else [])
    if "asr" in tasks and not asr:
        raise HTTPException(422, "transcripts need at least one ASR model")
    if "diar" in tasks and not seg.has_speakers:
        raise HTTPException(422, f"{seg.display_name} returns no speakers; pick a diariser for the speakers task")
    if body.strategy == "single" and body.single_model_id not in [m.id for m in asr]:
        raise HTTPException(422, "the single-model strategy needs one of the chosen ASR models")
    active = session.scalar(select(Run).where(Run.task == RunTask.label, Run.dataset_id == ds.id,
                                              Run.status.in_(ACTIVE)))
    if active is not None:
        raise HTTPException(409, f"a pseudo-label run is already {active.status} on this dataset (run {active.id})")
    pv = pseudo.preview(session, ds, seg, asr)
    run = Run(task=RunTask.label, dataset_id=ds.id, status="queued",
              params={"tasks": tasks, "seg_model_id": seg.id, "asr_model_ids": [m.id for m in asr],
                      "strategy": body.strategy, "single_model_id": body.single_model_id,
                      "high": body.high, "low": body.low,
                      "preview": {"saved_members": pv["saved_members"], "new_members": pv["new_members"]}})
    session.add(run)
    session.flush()
    session.refresh(run)
    job = jobs.enqueue(session, "label", {"run_id": run.id}, pseudo.required_services(pv))
    session.commit()
    return {"run": run_json(run), "job": job_brief(job)}


def _queue(session: Session, dataset_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Segment, AudioFile.rel_path).join(AudioFile).where(
            AudioFile.dataset_id == dataset_id, Segment.edited_at.is_(None),
            Segment.status.in_((SegmentStatus.pseudo, SegmentStatus.rejected)))
        .order_by(Segment.confidence.asc().nulls_first(), Segment.id).limit(limit)).all()
    return [{**edit.segment_json(s), "file": p} for s, p in rows]


@router.get("/datasets/{dataset_id}/results")
def label_results(dataset_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """The latest label run (its triage, agreement matrix, chosen shares), today's progress, and the review
    queue (unreviewed segments, lowest confidence first)."""
    ds = get_dataset(session, dataset_id)
    run = console.latest_label_run(session, ds.id)
    job = None
    if run is not None:
        job = session.scalar(select(Job).where(Job.type == "label").where(Job.payload["run_id"].as_integer() == run.id)
                             .order_by(Job.id.desc()))
    names = {m.registry_name: m.display_name for m in session.scalars(select(Model))}
    prog = console.progress(session, [ds])[ds.id]
    return {"dataset": dataset_json(session, ds), "run": run_json(run) if run else None,
            "job": job_brief(job) if job else None,
            "summary": (run.outputs or {}).get("summary") if run else None, "model_names": names,
            "progress": prog, "next": console.next_action(ds, prog), "queue": _queue(session, ds.id)}


# ---------------------------------------------------------------- annotate

def _file(session: Session, file_id: int) -> AudioFile:
    f = session.get(AudioFile, file_id)
    if f is None:
        raise HTTPException(404, "audio file not found")
    return f


def _segment(session: Session, segment_id: int) -> Segment:
    s = session.get(Segment, segment_id)
    if s is None:
        raise HTTPException(404, "segment not found")
    return s


def _segments(session: Session, ids: list[int]) -> list[Segment]:
    segs = list(session.scalars(select(Segment).where(Segment.id.in_(ids))))
    if len(segs) != len(set(ids)):
        raise HTTPException(404, "some segments were not found")
    return segs


@router.get("/datasets/{dataset_id}/files")
def label_files(dataset_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    get_dataset(session, dataset_id)
    return edit.file_counts(session, dataset_id)


@router.get("/files/{file_id}")
def annotate_file(file_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return edit.file_view(session, _file(session, file_id))


class SegmentPatch(BaseModel):
    text: str | None = None
    speaker: str | None = None
    start: float | None = None
    end: float | None = None
    status: str | None = None
    flagged: bool | None = None
    flag_note: str | None = None


def _commit_or_400(session: Session, fn):
    try:
        out = fn()
    except edit.EditError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from None
    session.commit()
    return out


@router.patch("/segments/{segment_id}")
def patch_segment(segment_id: int, body: SegmentPatch, session: Session = Depends(get_session)) -> dict[str, Any]:
    s = _segment(session, segment_id)
    fields = body.model_dump(exclude_unset=True)
    _commit_or_400(session, lambda: edit.update(session, s, fields))
    return edit.segment_json(s)


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1)
    action: str  # confirm | reject | review | assign_speaker | flag | unflag
    speaker: str | None = None


@router.post("/segments/bulk")
def bulk_segments(body: BulkIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    segs = _segments(session, body.ids)
    n = _commit_or_400(session, lambda: edit.bulk(session, segs, body.action, body.speaker))
    return {"updated": n, "segments": [edit.segment_json(s) for s in segs]}


class MergeIn(BaseModel):
    ids: list[int] = Field(min_length=2)


@router.post("/segments/merge")
def merge_segments(body: MergeIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    segs = _segments(session, body.ids)
    s = _commit_or_400(session, lambda: edit.merge(session, segs))
    return edit.segment_json(s)


class SplitIn(BaseModel):
    t: float


@router.post("/segments/{segment_id}/split")
def split_segment(segment_id: int, body: SplitIn, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    s = _segment(session, segment_id)
    a, b = _commit_or_400(session, lambda: edit.split(session, s, body.t))
    return [edit.segment_json(a), edit.segment_json(b)]


@router.delete("/segments/{segment_id}", status_code=204)
def delete_segment(segment_id: int, session: Session = Depends(get_session)) -> None:
    session.delete(_segment(session, segment_id))
    session.commit()


class SegmentIn(BaseModel):
    start: float
    end: float
    speaker: str | None = None
    text: str | None = None


@router.post("/files/{file_id}/segments", status_code=201)
def create_segment(file_id: int, body: SegmentIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    f = _file(session, file_id)
    s = _commit_or_400(session, lambda: edit.create(session, f, body.start, body.end, body.speaker, body.text))
    return edit.segment_json(s)


@router.post("/files/{file_id}/confirm-auto")
def confirm_auto(file_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    f = _file(session, file_id)
    return {"confirmed": _commit_or_400(session, lambda: edit.confirm_auto(session, f))}


# ---------------------------------------------------------------- live evaluation, promotion

@router.get("/datasets/{dataset_id}/live")
def live_eval(dataset_id: int, normaliser: str | None = None, collar: float | None = None,
              session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = get_dataset(session, dataset_id)
    try:
        return live.live(session, ds, normaliser, collar)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from None


class PromoteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scope: str = "complete"
    tags: list[str] | None = None


@router.post("/datasets/{dataset_id}/promote", status_code=201)
def promote_dataset(dataset_id: int, body: PromoteIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    ds = get_dataset(session, dataset_id)
    try:
        new = promote.promote(session, ds, body.name, body.scope,
                              _clean_tags(body.tags) if body.tags is not None else None)
    except promote.PromoteError as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from None
    session.commit()
    return dataset_json(session, new)
