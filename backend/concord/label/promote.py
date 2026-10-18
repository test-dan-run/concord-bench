"""'Promote to benchmark dataset': confirmed segments become the references of a new dataset.

Only confirmed segments are copied; auto-accepted ones never become a benchmark reference unless a
person confirms them. The audio is shared (same sha256, so the derived files and the inference cache
are reused). With scope `complete` only fully reviewed files are taken, so DER is meaningful; scope
`any` takes every file with a confirmed segment and marks the dataset ASR-only.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from ..core.models import AudioFile, Dataset, LabelType, Segment, SegmentStatus, utcnow
from ..runs.executor import usable_files

SCOPES = ("complete", "any")


class PromoteError(ValueError):
    pass


def promote(session: Session, ds: Dataset, name: str, scope: str = "complete", tags: list[str] | None = None,
            ) -> Dataset:
    if scope not in SCOPES:
        raise PromoteError(f"scope must be one of {', '.join(SCOPES)}")
    picked: list[tuple[AudioFile, list[Segment]]] = []
    for f in usable_files(session, ds.id):
        conf = [s for s in f.segments if s.status == SegmentStatus.confirmed]
        if not conf:
            continue
        if scope == "complete" and not all(s.reviewed for s in f.segments):
            continue
        picked.append((f, sorted(conf, key=lambda s: (s.start, s.end))))
    if not picked:
        raise PromoteError("no files qualify: confirm some segments first" + (
            " (scope 'complete' needs every segment of a file reviewed)" if scope == "complete" else ""))
    new = Dataset(name=name.strip(), language=ds.language, tags=list(tags if tags is not None else ds.tags),
                  description=f"Promoted from {ds.name!r} ({scope} files, confirmed segments only).",
                  mode="annotated", status="ready")
    session.add(new)
    session.flush()
    by_task: dict[str, dict[str, Any]] = {}
    sr: Counter[int] = Counter()
    seconds = 0.0
    for f, segs in picked:
        nf = AudioFile(dataset_id=new.id, rel_path=f.rel_path, sha256=f.sha256, ext=f.ext, duration=f.duration,
                       orig_sr=f.orig_sr, channels=f.channels, status=f.status, meta=dict(f.meta or {}))
        session.add(nf)
        session.flush()
        for s in segs:
            session.add(Segment(audio_file_id=nf.id, start=s.start, end=s.end, speaker=s.speaker, text=s.text,
                                status=SegmentStatus.confirmed, confidence=s.confidence,
                                meta={"promoted_from": s.uid}))
            kind = "spk_asr" if s.speaker and s.text is not None else ("asr" if s.text is not None else "diar")
            t = by_task.setdefault(kind, {"records": 0, "files": set(), "formats": ["promoted"], "preview": []})
            t["records"] += 1
            t["files"].add(f.rel_path)
            if len(t["preview"]) < 3:
                t["preview"].append({"file": f.rel_path, "start": s.start, "end": s.end, "speaker": s.speaker,
                                     "text": s.text})
        sr[f.orig_sr or 0] += 1
        seconds += f.duration or 0.0
    kinds = set(by_task)
    labels = []
    if kinds & {"asr", "spk_asr"}:
        labels.append(LabelType.asr.value)
    if scope == "complete" and kinds & {"diar", "spk_asr"}:
        labels.append(LabelType.diar.value)
    new.label_types = labels
    new.report = {
        "files": len(picked), "usable_files": len(picked), "hours": seconds / 3600, "usable_hours": seconds / 3600,
        "sample_rates": {str(k): v for k, v in sorted(sr.items()) if k},
        "flagged": {"clipped": [], "undecodable": [], "too_long": []},
        "tasks": {k: {**v, "files": len(v["files"])} for k, v in by_task.items()},
        "unmatched": [], "audio_without_labels": [], "audio_without_labels_count": 0, "duplicate_stems": [],
        "parse_errors": [], "files_by_format": [], "has_speaker_text": "spk_asr" in kinds and scope == "complete",
        "promoted_from": ds.id, "promoted_scope": scope, "promoted_at": utcnow().isoformat(),
    }
    session.flush()
    return new
