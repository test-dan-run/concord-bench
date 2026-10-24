"""Every on-disk location Concord uses. The only module to change if storage ever moves (Q2).

With DATASET_STORE set, originals are read from the Dataset Catalog's store (DATASET_CATALOG_PLAN.md §4.1),
mounted read-only; derived audio, peaks and enhanced copies stay under CONCORD_DATA."""

from __future__ import annotations

from pathlib import Path

from .settings import get_settings


def data_root() -> Path:
    return get_settings().concord_data


def db_path() -> Path:
    return data_root() / "concord.db"


def store() -> Path | None:
    """The Dataset Catalog's store as mounted here (DATASET_STORE), or None when it is not set up."""
    return get_settings().dataset_store


def store_audio(sha256: str) -> Path | None:
    """The original in the store (`audio/<sha256[0:2]>/<sha256>`, no extension), or None when the store is not
    set up or does not have it."""
    root = store()
    if root is None:
        return None
    p = root / "audio" / sha256[:2] / sha256
    return p if p.is_file() else None


def store_version(dataset_id: str, n: int) -> Path | None:
    """A Dataset Catalog version's folder in the store, or None when the store is not set up."""
    root = store()
    return root / "versions" / dataset_id / str(n) if root is not None else None


def local_original(sha256: str, ext: str) -> Path:
    """Bench's own copy of an original: every original before the store, and `just move-originals`' source."""
    return data_root() / "originals" / f"{sha256}{ext}"


def original(sha256: str, ext: str) -> Path:
    """The original audio: in the store when DATASET_STORE is set and the store has it, else Bench's own copy."""
    return store_audio(sha256) or local_original(sha256, ext)


def derived(sha256: str) -> Path:
    """16 kHz mono FLAC used for every model call and for playback."""
    return data_root() / "derived" / f"{sha256}.flac"


def peaks(sha256: str) -> Path:
    return data_root() / "peaks" / f"{sha256}.json"


def uploads(upload_id: str) -> Path:
    return data_root() / "uploads" / upload_id


def reference_import(import_id: int) -> Path:
    """Working folder of an accepted reference package: the ingest source built from it."""
    return data_root() / "imports" / str(import_id)


def job_log(job_id: int) -> Path:
    return data_root() / "logs" / f"job-{job_id}.log"


def job_progress(job_id: int) -> Path:
    return data_root() / "logs" / f"job-{job_id}.progress.json"


def ensure_dirs() -> None:
    subs = ("derived", "peaks", "uploads", "logs") + (() if store() is not None else ("originals",))
    for sub in subs:
        (data_root() / sub).mkdir(parents=True, exist_ok=True)
