"""Every on-disk location Concord uses. The only module to change if storage ever moves (Q2)."""

from __future__ import annotations

from pathlib import Path

from .settings import get_settings


def data_root() -> Path:
    return get_settings().concord_data


def db_path() -> Path:
    return data_root() / "concord.db"


def original(sha256: str, ext: str) -> Path:
    return data_root() / "originals" / f"{sha256}{ext}"


def derived(sha256: str) -> Path:
    """16 kHz mono FLAC used for every model call and for playback."""
    return data_root() / "derived" / f"{sha256}.flac"


def peaks(sha256: str) -> Path:
    return data_root() / "peaks" / f"{sha256}.json"


def uploads(upload_id: str) -> Path:
    return data_root() / "uploads" / upload_id


def exports() -> Path:
    return data_root() / "exports"


def offline_kits() -> Path:
    """Default destination of offline kits (the export form can name any folder)."""
    return data_root() / "offline"


def snapshots() -> Path:
    """Segment snapshots taken before a label-package merge (undo)."""
    return data_root() / "snapshots"


def imports(package_id: int) -> Path:
    """An imported label package, unpacked."""
    return data_root() / "imports" / str(package_id)


def install_id() -> Path:
    """A random id naming this install in label-package manifests."""
    return data_root() / "install_id"


def job_log(job_id: int) -> Path:
    return data_root() / "logs" / f"job-{job_id}.log"


def job_progress(job_id: int) -> Path:
    return data_root() / "logs" / f"job-{job_id}.progress.json"


def ensure_dirs() -> None:
    for sub in ("originals", "derived", "peaks", "uploads", "exports", "logs", "snapshots", "imports"):
        (data_root() / sub).mkdir(parents=True, exist_ok=True)
