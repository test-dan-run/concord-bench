"""The review console: label progress per dataset and the next action.

Progress is measured in audio time, so the bar adds up to the dataset's usable audio: each labelled
file's duration is split across its segments' statuses (by segment duration); a file with no segments
counts as `running` while a label run is active on its dataset, else `unlabelled`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.models import AudioFile, Dataset, Run, RunTask, Segment, SegmentStatus

BAR = ("confirmed", "auto", "review", "rejected", "running", "unlabelled")
STATUS_BAR = {SegmentStatus.confirmed: "confirmed", SegmentStatus.auto: "auto", SegmentStatus.pseudo: "review",
              SegmentStatus.rejected: "rejected"}
ACTIVE = ("queued", "running")


def latest_label_run(session: Session, dataset_id: int, done: bool = False) -> Run | None:
    stmt = select(Run).where(Run.task == RunTask.label, Run.dataset_id == dataset_id)
    if done:
        stmt = stmt.where(Run.status == "done")
    return session.scalar(stmt.order_by(Run.id.desc()))


def file_progress(duration: float, segs: list[tuple[str, float, float]]) -> dict[str, float]:
    """Seconds per bar status for one labelled file; `segs` = (status, start, end)."""
    seg_total = sum(max(0.0, e - s) for _, s, e in segs)
    out = dict.fromkeys(BAR, 0.0)
    if seg_total <= 0:
        return out
    for st, s, e in segs:
        out[STATUS_BAR.get(st, "review")] += duration * max(0.0, e - s) / seg_total
    return out


def progress(session: Session, datasets: list[Dataset]) -> dict[int, dict[str, Any]]:
    ids = [d.id for d in datasets]
    files = session.execute(select(AudioFile.id, AudioFile.dataset_id, AudioFile.duration, AudioFile.status)
                            .where(AudioFile.dataset_id.in_(ids))).all()
    usable = {fid: (ds, dur or 0.0) for fid, ds, dur, st in files if st in ("ok", "clipped")}
    segs: dict[int, list[tuple[str, float, float]]] = defaultdict(list)
    counts: dict[int, dict[str, int]] = defaultdict(lambda: dict.fromkeys(STATUS_BAR.values(), 0))
    sources: dict[int, set[str]] = defaultdict(set)
    tasks: dict[int, set[str]] = defaultdict(set)
    flagged: dict[int, int] = defaultdict(int)
    rows = session.execute(select(Segment.audio_file_id, Segment.status, Segment.start, Segment.end,
                                  Segment.source_run_id, Segment.text, Segment.speaker, Segment.flagged, Segment.meta)
                           .join(AudioFile).where(AudioFile.dataset_id.in_(ids)))
    for fid, st, s, e, src, text, spk, flag, meta in rows:
        if fid not in usable:
            continue
        ds = usable[fid][0]
        segs[fid].append((st, s, e))
        counts[ds][STATUS_BAR.get(st, "review")] += 1
        machine = src is not None or (meta or {}).get("source") == "pseudo-label"
        sources[ds].add("pseudo" if machine else "reference")
        if text is not None:
            tasks[ds].add("asr")
        if spk is not None:
            tasks[ds].add("diar")
        flagged[ds] += bool(flag)
    active = set(session.scalars(select(Run.dataset_id).where(Run.task == RunTask.label, Run.status.in_(ACTIVE),
                                                              Run.dataset_id.in_(ids))))
    out: dict[int, dict[str, Any]] = {}
    for d in datasets:
        bar = dict.fromkeys(BAR, 0.0)
        files_by_state = dict.fromkeys(("labelled", "unlabelled"), 0)
        for fid, (ds, dur) in usable.items():
            if ds != d.id:
                continue
            if segs.get(fid):
                for k, v in file_progress(dur, segs[fid]).items():
                    bar[k] += v
                files_by_state["labelled"] += 1
            else:
                bar["running" if d.id in active else "unlabelled"] += dur
                files_by_state["unlabelled"] += 1
        out[d.id] = {"bar": bar, "seconds": sum(bar.values()), "segments": counts[d.id],
                     "files": files_by_state, "sources": sorted(sources[d.id]), "tasks": sorted(tasks[d.id]),
                     "flagged": flagged[d.id], "running": d.id in active}
    return out


def next_action(d: Dataset, p: dict[str, Any]) -> dict[str, Any]:
    c = p["segments"]
    if p["running"]:
        return {"kind": "running", "label": "Pseudo-labelling…"}
    if not sum(c.values()):
        return {"kind": "pseudo", "label": "Pseudo-label"}
    if c["review"]:
        return {"kind": "review", "label": f"Review {c['review']}", "status": "pseudo"}
    if c["rejected"] and "pseudo" in p["sources"]:
        return {"kind": "review", "label": f"Check {c['rejected']} rejected", "status": "rejected"}
    if c["auto"]:
        return {"kind": "review", "label": f"Confirm {c['auto']} auto-accepted", "status": "auto"}
    if p["files"]["unlabelled"]:
        return {"kind": "pseudo", "label": f"Pseudo-label {p['files']['unlabelled']} more files"}
    return {"kind": "live", "label": "Live evaluation"}


def console(session: Session) -> list[dict[str, Any]]:
    datasets = list(session.scalars(select(Dataset).where(Dataset.status == "ready").order_by(Dataset.created_at.desc())))
    prog = progress(session, datasets)
    out = []
    for d in datasets:
        p = prog[d.id]
        source = "pseudo-labels" if "pseudo" in p["sources"] else ("uploaded references" if p["sources"] else
                                                                   "audio only")
        run = latest_label_run(session, d.id)
        out.append({"id": d.id, "name": d.name, "tags": d.tags, "language": d.language, "mode": d.mode,
                    "label_types": d.label_types, "label_source": source, "tasks": p["tasks"],
                    "hours": (d.report or {}).get("usable_hours"), "progress": p,
                    "next": next_action(d, p),
                    "label_run": {"id": run.id, "status": run.status} if run else None,
                    "created_at": d.created_at.isoformat()})
    return out
