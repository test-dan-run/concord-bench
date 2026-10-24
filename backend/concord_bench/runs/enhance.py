"""Enhanced copies of a dataset (EVALUATE_PLAN.md §5.2): one enhancement model's build over every usable
file, stored as derived audio under each output's own sha256.

- Files up to WHOLE_MAX_S go to the model whole; longer files go in CHUNK_S chunks with OVERLAP_S of
  overlap, joined with a linear crossfade (the audio travels as JSON tensors, ~50 MB each way for 6 min).
  The chunking is part of the copy's parameters, so changing it makes a different copy.
- Length check: the catalog verifies the length on its own check clip only, so every output (every chunk)
  is checked here. Within LENGTH_TOLERANCE samples (10 ms) it is padded or trimmed to the input's length and
  the shift is recorded; further off, the file fails, and a copy with failed files is `failed`.
- A copy is reused by every run that asks for the same (dataset, model, build, parameters). Deleting one
  removes its audio (unless another copy or an original has the same bytes) and keeps the row `deleted`.
- An output identical to its input is not stored again: the copy's file points at the original's audio.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.base import params_hash
from ..adapters.enhance import OIPEnhancer
from ..core import paths
from ..core.models import AudioFile, Dataset, EnhancedCopy, EnhancedFile, Model, utcnow
from ..datasets import audio as audio_mod

SR = audio_mod.TARGET_SR
WHOLE_MAX_S = 60.0
CHUNK_S = 30.0
OVERLAP_S = 0.5
LENGTH_TOLERANCE = 160  # samples: 10 ms at 16 kHz, the catalog's own limit
LIVE = ("building", "ready")

Log = Callable[[str], None]
Progress = Callable[[float, str], None]


class LengthError(Exception):
    """An enhancer returned audio more than LENGTH_TOLERANCE samples longer or shorter than its input."""


class Cancelled(Exception):
    pass


def copy_params(adapter: OIPEnhancer) -> dict[str, Any]:
    return {**adapter.params, "whole_max_s": WHOLE_MAX_S, "chunk_s": CHUNK_S, "overlap_s": OVERLAP_S}


def fix_length(y: np.ndarray, n: int) -> tuple[np.ndarray, int]:
    """`y` padded or trimmed to `n` samples, and how many samples it was off by. Raises LengthError past
    the tolerance."""
    diff = len(y) - n
    if abs(diff) > LENGTH_TOLERANCE:
        raise LengthError(f"{abs(diff) / SR * 1000:.1f} ms {'longer' if diff > 0 else 'shorter'} than the input "
                          f"(more than {LENGTH_TOLERANCE / SR * 1000:.0f} ms)")
    if diff > 0:
        y = y[:n]
    elif diff < 0:
        y = np.concatenate([y, np.zeros(-diff, dtype=y.dtype)])
    return y, abs(diff)


def chunks(n: int) -> list[tuple[int, int]]:
    """(start, end) sample ranges covering n samples: one range for short audio, else overlapping chunks."""
    if n <= WHOLE_MAX_S * SR:
        return [(0, n)]
    size, ov = int(CHUNK_S * SR), int(OVERLAP_S * SR)
    out, s = [], 0
    while True:
        e = min(n, s + size)
        out.append((s, e))
        if e >= n:
            return out
        s = e - ov


def enhance_audio(adapter: OIPEnhancer, x: np.ndarray) -> tuple[np.ndarray, float, int]:
    """(enhanced audio of exactly len(x) samples, latency seconds, largest length difference in samples)."""
    spans = chunks(len(x))
    ov = int(OVERLAP_S * SR)
    out = np.zeros(len(x), dtype=np.float32)
    latency, shift = 0.0, 0
    for k, (s, e) in enumerate(spans):
        y, lat = adapter.run(x[s:e])
        y, d = fix_length(y, e - s)
        latency += lat
        shift = max(shift, d)
        w = np.ones(e - s, dtype=np.float32)
        if k > 0:  # fade in over the overlap with the chunk before
            w[:ov] = np.linspace(0.0, 1.0, ov, endpoint=False, dtype=np.float32)
        if k < len(spans) - 1:  # fade out over the overlap with the chunk after
            w[-ov:] = np.linspace(1.0, 0.0, ov, endpoint=False, dtype=np.float32)[: min(ov, e - s)]
        out[s:e] += y * w
    return np.clip(out, -1.0, 1.0), latency, shift


def store_audio(y: np.ndarray) -> str:
    """Write enhanced audio as derived audio (16 kHz FLAC + peaks) under the sha256 of its bytes."""
    root = paths.data_root() / "derived"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=root, suffix=".enh.flac", delete=False) as fh:
        tmp = Path(fh.name)
    audio_mod.write_flac(tmp, y)
    sha = audio_mod.sha256_file(tmp)
    dest = paths.derived(sha)
    if dest.exists():
        tmp.unlink()
    else:
        tmp.replace(dest)
    if not paths.peaks(sha).exists():
        audio_mod.write_peaks(paths.peaks(sha), audio_mod.compute_peaks(y))
    return sha


# ---------------------------------------------------------------- finding copies

def find(session: Session, dataset_id: int, model_id: int, statuses: tuple[str, ...] = ("ready",),
         build_key: str | None = None) -> EnhancedCopy | None:
    """The newest copy of a dataset by an enhancement model in one of `statuses` (and that build)."""
    stmt = select(EnhancedCopy).where(EnhancedCopy.dataset_id == dataset_id, EnhancedCopy.model_id == model_id,
                                      EnhancedCopy.status.in_(statuses))
    if build_key:
        stmt = stmt.where(EnhancedCopy.build_key == build_key)
    return session.scalar(stmt.order_by(EnhancedCopy.id.desc()))


def files_of(session: Session, copy: EnhancedCopy) -> dict[int, EnhancedFile]:
    """audio_file id -> its enhanced file."""
    return {f.audio_file_id: f for f in session.scalars(select(EnhancedFile).where(EnhancedFile.copy_id == copy.id))}


def copy_json(c: EnhancedCopy, runs: int = 0) -> dict[str, Any]:
    return {"id": c.id, "dataset_id": c.dataset_id, "model_id": c.model_id, "model": c.model.catalog_id,
            "display_name": c.model.display_name, "generative": c.model.generative, "build_key": c.build_key,
            "status": c.status, "progress": c.progress, "error": c.error, "size_bytes": c.size_bytes,
            "files": c.files, "length_check": c.length_check, "max_shift_ms": c.max_shift_ms, "rtf": c.rtf,
            "audio_s": c.audio_s, "hardware": c.hardware or {}, "image": c.image, "job_id": c.job_id, "runs": runs,
            "created_at": c.created_at.isoformat(), "finished_at": c.finished_at.isoformat() if c.finished_at else None}


# ---------------------------------------------------------------- building

def build(session: Session, copy: EnhancedCopy, adapter: OIPEnhancer, log: Log, progress: Progress) -> EnhancedCopy:
    """Enhance every usable file of the copy's dataset not done yet. Stops when the copy is cancelled
    (another session sets its status) and removes what it wrote."""
    from .executor import usable_files

    ds: Dataset = session.get(Dataset, copy.dataset_id)
    files = usable_files(session, ds.id)
    done = files_of(session, copy)
    failed: list[str] = []
    for n, f in enumerate(files):
        session.refresh(copy, ["status"])
        if copy.status == "cancelled":
            raise Cancelled(f"the {copy.model.catalog_id} copy of {ds.name} was cancelled")
        progress(n / max(1, len(files)), f"{copy.model.catalog_id}: {f.rel_path}")
        copy.progress = n / max(1, len(files))
        if f.id in done:
            continue
        x = audio_mod.load_derived(paths.derived(f.sha256))
        try:
            y, latency, shift = enhance_audio(adapter, x)
        except LengthError as exc:
            failed.append(f"{f.rel_path}: {exc}")
            log(f"length check failed: {f.rel_path}: {exc}")
            continue
        # Unchanged audio (a no-op enhancer) points at the original's derived audio instead of a second copy.
        sha = f.sha256 if np.array_equal(y, np.clip(x, -1.0, 1.0)) else store_audio(y)
        session.add(EnhancedFile(copy_id=copy.id, audio_file_id=f.id, sha256=sha, duration=len(y) / SR,
                                 shift_ms=round(shift / SR * 1000, 3), latency_s=latency))
        session.commit()  # one file at a time: the API shares this SQLite file
    done = files_of(session, copy)
    shifts = [e.shift_ms for e in done.values()]
    copy.files = len(done)
    copy.audio_s = sum(e.duration for e in done.values())
    copy.processing_s = sum(e.latency_s for e in done.values())
    own = {f.id: f.sha256 for f in files}
    copy.size_bytes = sum(paths.derived(e.sha256).stat().st_size for e in done.values()
                          if e.sha256 != own.get(e.audio_file_id) and paths.derived(e.sha256).exists())
    copy.max_shift_ms = max(shifts) if shifts else 0.0
    copy.progress, copy.finished_at = 1.0, utcnow()
    if failed:
        copy.status, copy.length_check = "failed", "failed"
        copy.error = f"{len(failed)} file(s) failed the length check: " + "; ".join(failed[:5])
    else:
        copy.status, copy.length_check, copy.error = "ready", "trimmed" if any(shifts) else "ok", None
    session.commit()
    log(f"{copy.model.catalog_id} copy of {ds.name}: {copy.files} files, {copy.status}"
        + (f", largest length fix {copy.max_shift_ms:.1f} ms" if copy.max_shift_ms else ""))
    return copy


def new_copy(session: Session, ds: Dataset, model: Model, build: dict[str, Any], job_id: int | None) -> EnhancedCopy:
    adapter = OIPEnhancer(model.catalog_id)
    params = copy_params(adapter)
    c = EnhancedCopy(dataset_id=ds.id, model_id=model.id, build_key=build.get("build_key"),
                     image=build.get("image"), hardware=build.get("hardware") or {}, params=params,
                     params_hash=params_hash(params), status="building", job_id=job_id)
    session.add(c)
    session.flush()
    return c


# ---------------------------------------------------------------- deleting

def _in_use(session: Session, sha: str, copy_id: int) -> bool:
    other = session.scalar(select(EnhancedFile.id).join(EnhancedCopy).where(
        EnhancedFile.sha256 == sha, EnhancedFile.copy_id != copy_id, EnhancedCopy.status.in_(LIVE)))
    return other is not None or session.scalar(select(AudioFile.id).where(AudioFile.sha256 == sha)) is not None


def remove_audio(session: Session, copy: EnhancedCopy) -> int:
    """Delete a copy's files (keeping audio another copy or an original shares). Returns bytes freed."""
    freed = 0
    for e in list(session.scalars(select(EnhancedFile).where(EnhancedFile.copy_id == copy.id))):
        if not _in_use(session, e.sha256, copy.id):
            for p in (paths.derived(e.sha256), paths.peaks(e.sha256)):
                if p.exists():
                    freed += p.stat().st_size
                    p.unlink()
        session.delete(e)
    return freed


def delete(session: Session, copy: EnhancedCopy) -> int:
    freed = remove_audio(session, copy)
    copy.status, copy.size_bytes = "deleted", 0
    session.flush()
    return freed
