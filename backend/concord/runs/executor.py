"""Inference for one run: single-model ASR / seg runs and seg -> ASR pipelines.

Every model call goes through the output cache. `run.outputs` records which cache rows a run used,
per audio file, so scoring (and rescoring with another normaliser or collar) needs no inference.

Pipelines (PLAN §5 M4):
- the seg stage gives segments (model, or the dataset's own for the oracle);
- segments are merged (short ones into same-speaker neighbours) and padded, then each is transcribed;
- the oracle ASR outputs (reference segments) are also produced, so error attribution needs no
  inference beyond the oracle row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import adapters
from ..core import paths
from ..core.models import AudioFile, Dataset, Model, Run, RunTask, utcnow
from ..datasets.audio import cut, load_derived
from ..platform.services import image_info
from . import cache
from .references import FileRefs

Log = Callable[[str], None]
Progress = Callable[[float, str], None]

MERGE_GAP = 0.3  # same-speaker segments closer than this are joined
MIN_SEG = 0.5  # shorter segments are merged into the nearest same-speaker segment...
MERGE_MAX_GAP = 2.0  # ...when it is at most this far away
PAD = 0.1


class RunError(Exception):
    pass


def usable_files(session: Session, dataset_id: int, include_long: bool = False) -> list[AudioFile]:
    files = session.scalars(select(AudioFile).where(AudioFile.dataset_id == dataset_id)
                            .options(selectinload(AudioFile.segments)).order_by(AudioFile.rel_path)).all()
    return [f for f in files if f.usable or (include_long and f.status == "too_long")]


def file_refs(session: Session, dataset_id: int, include_long: bool = False) -> list[FileRefs]:
    return [FileRefs(f, list(f.segments)) for f in usable_files(session, dataset_id, include_long)]


def services_for(run: Run) -> list[str]:
    if run.task == RunTask.pipeline:
        p = run.pipeline
        assert p is not None
        names = [p.asr_model.registry_name]
        if p.seg_model is not None:
            names.append(p.seg_model.registry_name)
        return sorted(names)
    assert run.model is not None
    return [run.model.registry_name]


# ---------------------------------------------------------------- segment preparation

@dataclass
class Cut:
    start: float
    end: float
    speaker: str | None
    src: list[float]  # the original (unpadded) span


def prepare_cuts(segments: list[list[float]], speakers: list[str] | None, duration: float) -> list[Cut]:
    """Merge close / short same-speaker segments, then pad. Speech-only segments count as one speaker."""
    spk = speakers or ["speech"] * len(segments)
    by: dict[str, list[list[float]]] = {}
    for (s, e), k in zip(segments, spk, strict=True):
        if e > s:
            by.setdefault(k, []).append([max(0.0, s), min(duration, e)])
    cuts: list[Cut] = []
    for k, segs in by.items():
        segs.sort()
        merged: list[list[float]] = []
        for s, e in segs:
            if merged and s - merged[-1][1] < MERGE_GAP:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        changed = True
        while changed and len(merged) > 1:
            changed = False
            for i, (s, e) in enumerate(merged):
                if e - s >= MIN_SEG:
                    continue
                gaps = []
                if i > 0:
                    gaps.append((s - merged[i - 1][1], i - 1))
                if i + 1 < len(merged):
                    gaps.append((merged[i + 1][0] - e, i + 1))
                gap, j = min(gaps)
                if gap <= MERGE_MAX_GAP:
                    a, b = sorted((i, j))
                    merged[a] = [merged[a][0], max(merged[a][1], merged[b][1])]
                    del merged[b]
                    changed = True
                    break
        for s, e in merged:
            cuts.append(Cut(round(max(0.0, s - PAD), 3), round(min(duration, e + PAD), 3),
                            k if speakers else None, [s, e]))
    return sorted(cuts, key=lambda c: (c.start, c.end))


# ---------------------------------------------------------------- execution

def _load_audio(f: AudioFile) -> np.ndarray:
    return load_derived(paths.derived(f.sha256))


def _asr_on_spans(session: Session, adapter, f: AudioFile, audio: np.ndarray, spans: list[tuple[float, float]],
                  image: str | None) -> tuple[list[int], float, int]:
    ids, seconds, hits = [], 0.0, 0
    for s, e in spans:
        if e - s < 0.05:
            ids.append(None)
            continue
        whole = s <= 0.0 and e >= (f.duration or 0) - 1e-3
        row, hit = cache.get_or_run(session, adapter, audio if whole else cut(audio, s, e), f.sha256,
                                    0.0 if whole else s, cache.WHOLE if whole else e, image)
        ids.append(row.id)
        seconds += row.latency_s
        hits += hit
    return ids, seconds, hits


def execute(session: Session, run: Run, log: Log, progress: Progress) -> None:
    ds: Dataset = run.dataset
    refs = file_refs(session, ds.id, include_long=bool(run.params.get("include_long")))
    if not refs:
        raise RunError("the dataset has no usable audio files")
    run.status, run.started_at = "running", utcnow()
    run.image_tags = {n: image_info(n) for n in services_for(run)}
    session.commit()
    if run.task == RunTask.asr:
        _run_asr(session, run, refs, log, progress)
    elif run.task == RunTask.seg:
        _run_seg(session, run, refs, log, progress)
    elif run.task == RunTask.pipeline:
        _run_pipeline(session, run, refs, log, progress)
    else:
        raise RunError(f"task {run.task!r} is not available in this version")
    run.finished_at = utcnow()
    session.flush()


def _run_asr(session: Session, run: Run, refs: list[FileRefs], log: Log, progress: Progress) -> None:
    model: Model = run.model
    if not model.is_asr:
        raise RunError(f"{model.display_name} has no ASR capability")
    adapter = adapters.for_model(model, run.dataset.language)
    image = adapter.options.get("image")
    run.params = {**run.params, "adapter": adapter.params}
    outputs: dict[str, Any] = {}
    seconds = audio_s = 0.0
    hits = calls = 0
    for k, r in enumerate(refs):
        progress(k / len(refs), f"{model.display_name}: {r.file.rel_path}")
        audio = _load_audio(r.file)
        units = r.asr_units() if r.has_text else [None]
        spans = [(u.start, u.end) for u in units] if units[0] is not None else [(0.0, r.duration)]
        ids, sec, h = _asr_on_spans(session, adapter, r.file, audio, spans, image)
        outputs[str(r.file.id)] = {"units": [{"key": u.key if u else r.file.stem, "start": s, "end": e, "cache_id": i}
                                             for u, (s, e), i in zip(units, spans, ids, strict=True)]}
        seconds += sec
        audio_s += r.duration
        hits += h
        calls += len(spans)
        session.commit()  # keep cache rows, release the write lock
    run.outputs = outputs
    run.rtf_by_stage = {"asr": seconds / audio_s if audio_s else None, "total": seconds / audio_s if audio_s else None,
                        "audio_s": audio_s, "processing_s": seconds}
    log(f"ASR: {calls} calls, {hits} from cache, RTF {run.rtf_by_stage['asr'] or 0:.3f}")


def _run_seg(session: Session, run: Run, refs: list[FileRefs], log: Log, progress: Progress) -> None:
    model: Model = run.model
    if not model.is_seg:
        raise RunError(f"{model.display_name} has no VAD / diarisation capability")
    adapter = adapters.for_model(model)
    image = adapter.options.get("image")
    run.params = {**run.params, "adapter": adapter.params}
    outputs: dict[str, Any] = {}
    seconds = audio_s = 0.0
    hits = 0
    for k, r in enumerate(refs):
        progress(k / len(refs), f"{model.display_name}: {r.file.rel_path}")
        row, hit = cache.get_or_run(session, adapter, _load_audio(r.file), r.file.sha256, image=image)
        outputs[str(r.file.id)] = {"cache_id": row.id}
        seconds += row.latency_s
        audio_s += r.duration
        hits += hit
        session.commit()  # keep cache rows, release the write lock
    run.outputs = outputs
    run.rtf_by_stage = {"seg": seconds / audio_s if audio_s else None, "total": seconds / audio_s if audio_s else None,
                        "audio_s": audio_s, "processing_s": seconds}
    log(f"VAD / diarisation: {len(refs)} files, {hits} from cache, RTF {run.rtf_by_stage['seg'] or 0:.3f}")


def oracle_segments(r: FileRefs) -> tuple[list[list[float]], list[str]]:
    """The dataset's own segments and speakers: speaker-attributed text segments when present,
    otherwise speaker turns."""
    src = r.spk_text or [s for s in r.segments if s.speaker is not None]
    src = sorted(src, key=lambda s: (s.start, s.end))
    return [[s.start, s.end] for s in src], [s.speaker for s in src]


def _run_pipeline(session: Session, run: Run, refs: list[FileRefs], log: Log, progress: Progress) -> None:
    p = run.pipeline
    asr_model, seg_model = p.asr_model, p.seg_model
    if not asr_model.is_asr:
        raise RunError(f"{asr_model.display_name} has no ASR capability")
    if seg_model is not None and not seg_model.is_seg:
        raise RunError(f"{seg_model.display_name} has no VAD / diarisation capability")
    asr = adapters.for_model(asr_model, run.dataset.language)
    seg = adapters.for_model(seg_model) if seg_model else None
    asr_image = asr.options.get("image")
    seg_image = seg.options.get("image") if seg else None
    run.params = {**run.params, "asr_adapter": asr.params, "seg_adapter": seg.params if seg else "oracle",
                  "merge_gap": MERGE_GAP, "min_seg": MIN_SEG, "pad": PAD}
    outputs: dict[str, Any] = {}
    seg_s = asr_s = audio_s = 0.0
    calls = hits = 0
    for k, r in enumerate(refs):
        progress(k / len(refs), f"{seg_model.display_name if seg_model else 'oracle'} → {asr_model.display_name}: "
                 f"{r.file.rel_path}")
        audio = _load_audio(r.file)
        entry: dict[str, Any] = {}
        # seg stage
        if seg is not None:
            row, hit = cache.get_or_run(session, seg, audio, r.file.sha256, image=seg_image)
            entry["seg_cache_id"] = row.id
            seg_s += row.latency_s
            segs, spks = row.output["segments"], row.output.get("speakers")
        else:
            segs, spks = oracle_segments(r)
            if not segs:
                outputs[str(r.file.id)] = {"skipped": "no reference segments for the oracle stage"}
                continue
        cuts = prepare_cuts(segs, spks, r.duration)
        ids, sec, h = _asr_on_spans(session, asr, r.file, audio, [(c.start, c.end) for c in cuts], asr_image)
        asr_s += sec
        calls += len(cuts)
        hits += h
        entry["cuts"] = [{"start": c.start, "end": c.end, "speaker": c.speaker, "src": c.src, "cache_id": i}
                         for c, i in zip(cuts, ids, strict=True)]
        # oracle ASR outputs for error attribution (A), shared with the oracle pipeline through the cache
        if seg is not None and r.has_spk_text:
            o_segs, o_spks = oracle_segments(r)
            o_cuts = prepare_cuts(o_segs, o_spks, r.duration)
            o_ids, _, oh = _asr_on_spans(session, asr, r.file, audio, [(c.start, c.end) for c in o_cuts], asr_image)
            calls += len(o_cuts)
            hits += oh
            entry["oracle_cuts"] = [{"start": c.start, "end": c.end, "speaker": c.speaker, "src": c.src,
                                     "cache_id": i} for c, i in zip(o_cuts, o_ids, strict=True)]
        outputs[str(r.file.id)] = entry
        audio_s += r.duration
        session.commit()  # keep cache rows, release the write lock
    run.outputs = outputs
    rtf = {"seg": seg_s / audio_s if audio_s and seg else (0.0 if audio_s else None),
           "asr": asr_s / audio_s if audio_s else None}
    rtf["total"] = (rtf["seg"] or 0.0) + (rtf["asr"] or 0.0) if audio_s else None
    run.rtf_by_stage = {**rtf, "audio_s": audio_s, "processing_s": seg_s + asr_s}
    log(f"pipeline: {calls} ASR calls ({hits} from cache), RTF {rtf['total'] or 0:.3f}")
