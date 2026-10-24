"""Inference: single-model ASR / seg runs, the stages of a pipeline grid, and pipeline assembly.

Every model call goes through the output cache (`runs.cache`), which also asks the model catalog's
output index, so an output either Concord app already has is not computed again. `run.outputs` records
which cache rows a run used, per audio file, so scoring (and rescoring with another normaliser or collar)
needs no inference.

Pipelines run stage by stage (MODEL_CATALOG_REDESIGN.md, "Job planning"), so each model loads once:
- `seg_stage`: one segmenter over every usable file of the dataset (whole-file outputs);
- `asr_stage`: one ASR model over the cuts of every seg source of the grid (a segmenter's stored outputs,
  or the dataset's own segments for the oracle), plus the oracle cuts error attribution uses;
- `execute` on a pipeline run then assembles it from stored outputs (`infer=False`): no model.
Segments are merged (short ones into same-speaker neighbours) and padded before transcription (PLAN §5 M4).

Calls overlap (`concurrency_for`), so an output's `latency_s` is its share of the wall time spent computing,
not its request's own latency; RTF sums those shares (`_compute`).

`builds` say which build of each model an output must come from: catalog model id -> {build_key, image}.

Speech enhancement (EVALUATE_PLAN.md §5): a run, stage or pipeline stage can read an enhanced copy of the
dataset instead of the original audio. `with_copy` points each file's refs at the copy's file, whose own
sha256 keys the cache, so everything downstream is unchanged. A pipeline enhanced "before" diarisation
reads the copy in both stages; "after", diarisation reads the original and ASR transcribes the copy cut at
the diarised segments (the copy has the original's length, so the segment times hold).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import adapters
from ..adapters.base import AudioRef
from ..core import paths
from ..core.models import AudioFile, Dataset, EnhancedCopy, Model, ModelOutput, Run, RunTask, utcnow
from ..core.settings import get_settings
from ..datasets.audio import cut, load_derived
from . import cache
from .cache import WHOLE, Key, OutputCache
from .references import FileRefs

Log = Callable[[str], None]
Progress = Callable[[float, str], None]
Builds = dict[str, dict[str, Any]]

MERGE_GAP = 0.3  # same-speaker segments closer than this are joined
MIN_SEG = 0.5  # shorter segments are merged into the nearest same-speaker segment...
MERGE_MAX_GAP = 2.0  # ...when it is at most this far away
PAD = 0.1
MIN_SPAN = 0.05  # shorter spans are not sent to a model

# Calls in flight per model (settings `infer_concurrency` overrides): vLLM batches requests that arrive
# together; the custom servers take one at a time behind a lock, so a second call only overlaps the upload.
CONCURRENCY = {"vllm": 8, "default": 2}
INDEX_BATCH = 100  # stored rows per index record call


class RunError(Exception):
    pass


class MissingOutput(RunError):
    """Assembling a pipeline found no stored output where its stage should have left one."""


def usable_files(session: Session, dataset_id: int) -> list[AudioFile]:
    files = session.scalars(select(AudioFile).where(AudioFile.dataset_id == dataset_id)
                            .options(selectinload(AudioFile.segments)).order_by(AudioFile.rel_path)).all()
    return [f for f in files if f.usable]


def file_refs(session: Session, dataset_id: int) -> list[FileRefs]:
    return [FileRefs(f, list(f.segments)) for f in usable_files(session, dataset_id)]


def ready_copy(session: Session, dataset_id: int, enh_model: Model) -> EnhancedCopy:
    """The enhanced copy a run or stage reads: the newest ready copy of the dataset by that model."""
    from . import enhance

    c = enhance.find(session, dataset_id, enh_model.id)
    if c is None:
        raise RunError(f"no ready {enh_model.display_name} copy of this dataset: build it first")
    return c


def with_copy(session: Session, refs: list[FileRefs], copy: EnhancedCopy | None) -> list[FileRefs]:
    """The same refs reading the copy's audio (None: the original)."""
    if copy is None:
        return refs
    from . import enhance

    files = enhance.files_of(session, copy)
    missing = [r.file.rel_path for r in refs if r.file.id not in files]
    if missing:
        raise RunError(f"the {copy.model.catalog_id} copy has no file for {len(missing)} usable file(s), e.g. "
                       f"{missing[0]}; build the copy again")
    return [FileRefs(r.file, r.segments, files[r.file.id].sha256) for r in refs]


def models_of(run: Run) -> list[Model]:
    """The models whose outputs a run uses (the enhancer's output is the copy, built before)."""
    if run.task == RunTask.pipeline:
        p = run.pipeline
        assert p is not None
        return [m for m in (p.seg_model, p.asr_model) if m is not None]
    assert run.model is not None
    return [run.model]


def services_for(run: Run) -> list[str]:
    """The catalog model ids a run calls."""
    return sorted(m.catalog_id for m in models_of(run))


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


def oracle_segments(r: FileRefs) -> tuple[list[list[float]], list[str]]:
    """The dataset's own segments and speakers: speaker-attributed text segments when present,
    otherwise speaker turns."""
    src = r.spk_text or [s for s in r.segments if s.speaker is not None]
    src = sorted(src, key=lambda s: (s.start, s.end))
    return [[s.start, s.end] for s in src], [s.speaker for s in src]


def oracle_cuts(r: FileRefs) -> list[Cut]:
    segs, spks = oracle_segments(r)
    return prepare_cuts(segs, spks, r.duration) if segs else []


def seg_cuts(r: FileRefs, row: ModelOutput) -> list[Cut]:
    return prepare_cuts(row.output["segments"], row.output.get("speakers"), r.duration)


# ---------------------------------------------------------------- calls through the cache

def span_key(r: FileRefs, start: float, end: float) -> Key | None:
    """The cache key of a span of a file: the whole file when it covers it, None when too short to send."""
    if end - start < MIN_SPAN:
        return None
    if start <= 0.0 and end >= (r.file.duration or 0) - 1e-3:
        return (r.sha256, WHOLE, WHOLE)
    return cache.key(r.sha256, start, end)


def seg_work(refs: list[FileRefs]) -> list[tuple[FileRefs, list[Key]]]:
    return [(r, [(r.sha256, WHOLE, WHOLE)]) for r in refs]


def _load_audio(r: FileRefs) -> np.ndarray:
    return load_derived(paths.derived(r.sha256))


def concurrency_for(model: Model) -> int:
    """Calls kept in flight for a model: settings `infer_concurrency` merged over CONCURRENCY, by catalog id,
    then engine, then "default"."""
    c = {**CONCURRENCY, **get_settings().infer_concurrency}
    n = c.get(model.catalog_id) or c.get((model.catalog or {}).get("engine") or "") or c.get("default") or 1
    return max(1, int(n))


def infer(session: Session, oc: OutputCache, adapter: adapters.Adapter, build: dict[str, Any],
          work: list[tuple[FileRefs, list[Key]]], progress: Progress, label: str,
          mode: str = "run", concurrency: int = 1) -> tuple[dict[Key, ModelOutput], dict[str, int]]:
    """Every (file, span) of `work` for one model: stored rows, plus calls for what is missing.

    mode `run` computes what is missing (`_compute`, up to `concurrency` calls at once), `lookup` raises
    MissingOutput instead, `check` only counts it. The index is asked once for the whole work list."""
    bk = build.get("build_key")
    work = [(r, list(dict.fromkeys(keys))) for r, keys in work]
    every = [k for _, keys in work for k in keys]
    pre = oc.prefetch(adapter, bk, every)
    session.commit()  # rows copied from other apps
    oc.flush()
    rows = oc.lookup_many(adapter, bk, every)
    todo: list[tuple[FileRefs, list[Key]]] = []
    seen: set[Key] = set()  # files with the same audio share their outputs
    for r, keys in work:
        missing = [k for k in keys if k not in rows and k not in seen]
        seen.update(missing)
        if missing:
            todo.append((r, missing))
    stats = {"calls": len(every), "hits": 0, "computed": 0, "missing": 0, "copied": pre["copied"]}
    if todo and mode == "check":
        stats["missing"] = len(seen)
    elif todo and mode == "lookup":
        r, missing = todo[0]
        raise MissingOutput(f"{adapter.service} has no stored output for {r.file.rel_path} "
                            f"({len(missing)} span(s)); run its stage again")
    elif todo:
        computed, stats["computed"] = _compute(session, oc, adapter, build, todo, progress, label, concurrency)
        rows.update(computed)
    stats["hits"] = stats["calls"] - stats["computed"] - stats["missing"]
    return rows, stats


def _compute(session: Session, oc: OutputCache, adapter: adapters.Adapter, build: dict[str, Any],
             todo: list[tuple[FileRefs, list[Key]]], progress: Progress, label: str,
             concurrency: int) -> tuple[dict[Key, ModelOutput], int]:
    """Calls for every missing span, up to `concurrency` in flight. Only `adapter.run` runs in the threads;
    rows are stored, committed and indexed here as calls finish. Returns the rows and how many were new
    (a row another process stored first is used instead).

    A row's `latency_s` is its share of the job's wall time, so RTF (a sum over rows) stays wall time when
    calls overlap: the seconds since the previous calls finished, split by request latency. Replayed rows
    keep their recorded latency."""
    bk, image = build.get("build_key"), build.get("image")
    wall = get_settings().replay_dir is None
    if wall:
        adapter.client()  # made once, before the threads share it
    calls = ((r, k) for r, keys in todo for k in keys)
    total = sum(len(keys) for _, keys in todo)
    rows: dict[Key, ModelOutput] = {}
    new = done = 0
    audio: tuple[str, np.ndarray] | None = None  # the current file's; calls come file by file

    def submit(pool: ThreadPoolExecutor, r: FileRefs, k: Key) -> Future:
        nonlocal audio
        if audio is None or audio[0] != r.sha256:
            audio = (r.sha256, _load_audio(r))
        _, s, e = k
        return pool.submit(adapter.run, audio[1] if e < 0 else cut(audio[1], s, e), AudioRef(r.sha256, s, e))

    pool = ThreadPoolExecutor(concurrency, thread_name_prefix="infer")
    running: dict[Future, tuple[FileRefs, Key]] = {}
    mark = time.perf_counter()
    try:
        while True:
            while len(running) < concurrency and (nxt := next(calls, None)) is not None:
                running[submit(pool, *nxt)] = nxt
            if not running:
                break
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            now = time.perf_counter()
            results = [(running.pop(f), f) for f in finished]
            ok = [(rk, f.result()) for rk, f in results if f.exception() is None]
            latency = sum(lat for _, (_, lat) in ok)
            share = (now - mark) / latency if wall and latency > 0 else 1.0
            mark = now
            for (_, k), (output, lat) in ok:
                row = cache.new_row(adapter, k, output, lat * share, bk, image)
                rows[k] = oc.store(row)
                new += rows[k] is row
            session.commit()  # keep finished rows, release the write lock
            if oc.pending >= INDEX_BATCH:
                oc.flush()
            errors = [exc for _, f in results if (exc := f.exception()) is not None]
            if errors:
                raise errors[0]
            done += len(finished)
            progress(done / total, f"{label}: {results[-1][0][0].file.rel_path}")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        oc.flush()
    return rows, new


def _build(builds: Builds, model: Model) -> dict[str, Any]:
    b = builds.get(model.catalog_id)
    if not b or not b.get("build_key"):
        raise RunError(f"no build key for {model.catalog_id}: the catalog has not finished identifying its build")
    return b


# ---------------------------------------------------------------- runs

def execute(session: Session, run: Run, log: Log, progress: Progress, builds: Builds, infer_missing: bool = True) -> None:
    """A run's inference (single runs) or assembly from stored outputs (pipelines, `infer_missing=False`)."""
    ds: Dataset = run.dataset
    refs = file_refs(session, ds.id)
    if not refs:
        raise RunError("the dataset has no usable audio files")
    for m in models_of(run):
        _build(builds, m)
    run.status, run.started_at = "running", utcnow()
    used = {m.catalog_id: {"build_key": builds[m.catalog_id]["build_key"], "image": builds[m.catalog_id].get("image")}
            for m in models_of(run)}
    hardware = {m.catalog_id: builds[m.catalog_id].get("hardware") or {} for m in models_of(run)}
    copy = ready_copy(session, ds.id, run.enh_model) if run.enh_model is not None else None
    if copy is not None:
        run.enh_copy_id = copy.id
        used[copy.model.catalog_id] = {"build_key": copy.build_key, "image": copy.image, "copy_id": copy.id}
        hardware[copy.model.catalog_id] = copy.hardware or {}
    run.builds, run.hardware = used, hardware  # assigned whole: JSON columns do not track changes inside
    session.commit()
    oc = OutputCache(session, log)
    mode = "run" if infer_missing else "lookup"
    if run.task == RunTask.asr:
        _run_asr(session, oc, run, with_copy(session, refs, copy), builds, log, progress, mode)
    elif run.task == RunTask.seg:
        _run_seg(session, oc, run, with_copy(session, refs, copy), builds, log, progress, mode)
    elif run.task == RunTask.pipeline:
        seg_copy = copy if run.enh_placement != "after" else None
        _run_pipeline(session, oc, run, with_copy(session, refs, seg_copy), with_copy(session, refs, copy), builds,
                      log, progress, mode)
    else:
        raise RunError(f"task {run.task!r} is not available in this version")
    run.finished_at = utcnow()
    session.flush()


def _rtf(seconds: float, audio_s: float) -> float | None:
    return seconds / audio_s if audio_s else None


def _enh_rtf(session: Session, run: Run) -> dict[str, Any]:
    """The enhancer's RTF, kept apart: the copy is built once per dataset and shared by every run on it,
    so `total` stays the model's own."""
    c = session.get(EnhancedCopy, run.enh_copy_id) if run.enh_copy_id else None
    return {"enh": c.rtf} if c is not None else {}


def _run_asr(session: Session, oc: OutputCache, run: Run, refs: list[FileRefs], builds: Builds, log: Log,
             progress: Progress, mode: str) -> None:
    model: Model = run.model
    if not model.is_asr:
        raise RunError(f"{model.display_name} has no ASR capability")
    adapter = adapters.for_model(model, run.dataset.language)
    run.params = {**run.params, "adapter": adapter.params}
    units = {}
    for r in refs:
        us = r.asr_units() if r.has_text else [None]
        units[r.file.id] = [(u, (u.start, u.end) if u is not None else (0.0, r.duration)) for u in us]
    work = [(r, [k for _, (s, e) in units[r.file.id] if (k := span_key(r, s, e))]) for r in refs]
    rows, st = infer(session, oc, adapter, _build(builds, model), work, progress, model.display_name, mode,
                     concurrency_for(model))
    outputs: dict[str, Any] = {}
    seconds = audio_s = 0.0
    calls = 0
    for r in refs:
        entry = []
        for u, (s, e) in units[r.file.id]:
            k = span_key(r, s, e)
            row = rows.get(k) if k else None
            entry.append({"key": u.key if u else r.file.stem, "start": s, "end": e, "cache_id": row.id if row else None})
            seconds += row.latency_s if row else 0.0
            calls += 1
        outputs[str(r.file.id)] = {"units": entry}
        audio_s += r.duration
    run.outputs = outputs
    rtf = _rtf(seconds, audio_s)
    run.rtf_by_stage = {"asr": rtf, "total": rtf, "audio_s": audio_s, "processing_s": seconds, **_enh_rtf(session, run)}
    log(f"ASR: {calls} calls, {st['hits']} from cache, RTF {rtf or 0:.3f}")


def _run_seg(session: Session, oc: OutputCache, run: Run, refs: list[FileRefs], builds: Builds, log: Log,
             progress: Progress, mode: str) -> None:
    model: Model = run.model
    if not model.is_seg:
        raise RunError(f"{model.display_name} has no VAD / diarisation capability")
    adapter = adapters.for_model(model)
    run.params = {**run.params, "adapter": adapter.params}
    rows, st = infer(session, oc, adapter, _build(builds, model), seg_work(refs), progress, model.display_name, mode,
                     concurrency_for(model))
    outputs: dict[str, Any] = {}
    seconds = audio_s = 0.0
    for r in refs:
        row = rows[(r.sha256, WHOLE, WHOLE)]
        outputs[str(r.file.id)] = {"cache_id": row.id}
        seconds += row.latency_s
        audio_s += r.duration
    run.outputs = outputs
    rtf = _rtf(seconds, audio_s)
    run.rtf_by_stage = {"seg": rtf, "total": rtf, "audio_s": audio_s, "processing_s": seconds, **_enh_rtf(session, run)}
    log(f"VAD / diarisation: {len(refs)} files, {st['hits']} from cache, RTF {rtf or 0:.3f}")


def _run_pipeline(session: Session, oc: OutputCache, run: Run, refs: list[FileRefs], asr_refs: list[FileRefs],
                  builds: Builds, log: Log, progress: Progress, mode: str) -> None:
    """`refs` is the audio diarisation hears, `asr_refs` the audio ASR transcribes (both the same files)."""
    p = run.pipeline
    asr_model, seg_model = p.asr_model, p.seg_model
    if not asr_model.is_asr:
        raise RunError(f"{asr_model.display_name} has no ASR capability")
    if seg_model is not None and not seg_model.is_seg:
        raise RunError(f"{seg_model.display_name} has no VAD / diarisation capability")
    asr = adapters.for_model(asr_model, run.dataset.language)
    seg = adapters.for_model(seg_model) if seg_model else None
    run.params = {**run.params, "asr_adapter": asr.params, "seg_adapter": seg.params if seg else "oracle",
                  "merge_gap": MERGE_GAP, "min_seg": MIN_SEG, "pad": PAD}
    seg_name = seg_model.display_name if seg_model else "oracle"
    seg_rows: dict[Key, ModelOutput] = {}
    if seg is not None:
        seg_rows, _ = infer(session, oc, seg, _build(builds, seg_model), seg_work(refs),
                            lambda f, m: progress(0.3 * f, m), seg_name, mode, concurrency_for(seg_model))
    outputs: dict[str, Any] = {}
    plans = []
    asr_view = {a.file.id: a for a in asr_refs}
    for r_seg in refs:
        r = asr_view[r_seg.file.id]
        seg_row = seg_rows.get((r_seg.sha256, WHOLE, WHOLE)) if seg is not None else None
        cuts = seg_cuts(r, seg_row) if seg_row is not None else oracle_cuts(r)
        if seg is None and not cuts:
            outputs[str(r.file.id)] = {"skipped": "no reference segments for the oracle stage"}
            continue
        # oracle ASR outputs for error attribution (A), shared with the oracle pipeline through the cache
        o_cuts = oracle_cuts(r) if seg is not None and r.has_spk_text else None
        plans.append((r, seg_row, cuts, o_cuts))
    work = [(r, [k for c in cuts + (o_cuts or []) if (k := span_key(r, c.start, c.end))])
            for r, _, cuts, o_cuts in plans]
    rows, st = infer(session, oc, asr, _build(builds, asr_model), work, lambda f, m: progress(0.3 + 0.7 * f, m),
                     f"{seg_name} → {asr_model.display_name}", mode, concurrency_for(asr_model))

    def cut_json(r: FileRefs, cs: list[Cut]) -> tuple[list[dict[str, Any]], float]:
        out, sec = [], 0.0
        for c in cs:
            k = span_key(r, c.start, c.end)
            row = rows.get(k) if k else None
            out.append({"start": c.start, "end": c.end, "speaker": c.speaker, "src": c.src,
                        "cache_id": row.id if row else None})
            sec += row.latency_s if row else 0.0
        return out, sec

    seg_s = asr_s = audio_s = 0.0
    calls = 0
    for r, seg_row, cuts, o_cuts in plans:
        entry: dict[str, Any] = {}
        if seg_row is not None:
            entry["seg_cache_id"] = seg_row.id
            seg_s += seg_row.latency_s
        entry["cuts"], sec = cut_json(r, cuts)
        asr_s += sec
        calls += len(cuts)
        if o_cuts is not None:
            entry["oracle_cuts"], _ = cut_json(r, o_cuts)
            calls += len(o_cuts)
        outputs[str(r.file.id)] = entry
        audio_s += r.duration
    run.outputs = outputs
    rtf = {"seg": seg_s / audio_s if audio_s and seg else (0.0 if audio_s else None),
           "asr": asr_s / audio_s if audio_s else None}
    rtf["total"] = (rtf["seg"] or 0.0) + (rtf["asr"] or 0.0) if audio_s else None
    run.rtf_by_stage = {**rtf, "audio_s": audio_s, "processing_s": seg_s + asr_s, **_enh_rtf(session, run)}
    log(f"pipeline: {calls} ASR calls ({st['hits']} from cache), RTF {rtf['total'] or 0:.3f}")


# ---------------------------------------------------------------- stages of a grid

def seg_stage(session: Session, model: Model, ds: Dataset, build: dict[str, Any], log: Log, progress: Progress,
              mode: str = "run", enh_model: Model | None = None) -> dict[str, Any]:
    """One segmenter over every usable file of a dataset (whole files), on the original audio or an enhanced
    copy. mode `check` only counts what is missing."""
    if not model.is_seg:
        raise RunError(f"{model.display_name} has no VAD / diarisation capability")
    refs = with_copy(session, file_refs(session, ds.id), ready_copy(session, ds.id, enh_model) if enh_model else None)
    rows, st = infer(session, OutputCache(session, log), adapters.for_model(model), build, seg_work(refs), progress,
                     f"{model.display_name} (seg stage)", mode, concurrency_for(model))
    if mode == "run":
        log(f"seg stage {model.display_name}: {len(refs)} files, {st['hits']} stored ({st['copied']} from other "
            f"apps), {st['computed']} computed")
    return {"model": model.catalog_id, "build_key": build.get("build_key"), "image": build.get("image"),
            "hardware": build.get("hardware") or {}, "files": len(refs), **st}


@dataclass(frozen=True)
class Source:
    """Where an ASR stage's cuts come from: a segmenter (None = the oracle) and the audio it heard."""

    seg: Model | None
    seg_enh: Model | None = None


def asr_stage(session: Session, model: Model, ds: Dataset, sources: list[Source], seg_builds: Builds,
              build: dict[str, Any], log: Log, progress: Progress, mode: str = "run",
              enh_model: Model | None = None) -> dict[str, Any]:
    """One ASR model over the cuts of every seg source (None = the oracle) of a grid, plus the oracle cuts of
    files with speaker-attributed text (error attribution), on the original audio or an enhanced copy. Seg
    outputs must be stored already."""
    if not model.is_asr:
        raise RunError(f"{model.display_name} has no ASR capability")
    base = file_refs(session, ds.id)
    refs = with_copy(session, base, ready_copy(session, ds.id, enh_model) if enh_model else None)
    oc = OutputCache(session, log)
    keys: dict[int, list[Key]] = {r.file.id: [] for r in refs}

    def add(r: FileRefs, cuts: list[Cut]) -> None:
        keys[r.file.id] += [k for c in cuts if (k := span_key(r, c.start, c.end))]

    for src in sources:
        if src.seg is None:
            for r in refs:
                add(r, oracle_cuts(r))
            continue
        seg_refs = with_copy(session, base, ready_copy(session, ds.id, src.seg_enh) if src.seg_enh else None)
        seg_rows, _ = infer(session, oc, adapters.for_model(src.seg), _build(seg_builds, src.seg), seg_work(seg_refs),
                            progress, src.seg.display_name, "lookup")
        for r, rs in zip(refs, seg_refs, strict=True):
            add(r, seg_cuts(r, seg_rows[(rs.sha256, WHOLE, WHOLE)]))
            if r.has_spk_text:
                add(r, oracle_cuts(r))
    work = [(r, keys[r.file.id]) for r in refs if keys[r.file.id]]
    _, st = infer(session, oc, adapters.for_model(model, ds.language), build, work, progress,
                  f"{model.display_name} (ASR stage)", mode, concurrency_for(model))
    if mode == "run":
        log(f"ASR stage {model.display_name}: {st['calls']} cuts from {len(sources)} seg source(s), {st['hits']} "
            f"stored ({st['copied']} from other apps), {st['computed']} computed")
    return {"model": model.catalog_id, "build_key": build.get("build_key"), "image": build.get("image"),
            "hardware": build.get("hardware") or {}, "files": len(work), **st}
