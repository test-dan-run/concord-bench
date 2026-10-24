"""`just move-originals` (DATASET_CATALOG_PLAN.md §12): Bench's originals from before the Dataset Catalog go
into its store.

1. Every file in CONCORD_DATA/originals/ is uploaded with `upload_audio`, which checks its sha256 (the
   file name), and each one a Bench dataset uses is held.
2. originals/ is renamed originals.moved/, so originals are read from the store from then on.
3. The person deletes originals.moved/ when satisfied. Nothing here deletes audio.

A file the catalog refuses (its bytes don't match its name) is listed, and originals/ is then left in
place, so nothing is read from the store that isn't there. Running it again finishes the job.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import paths
from ..core.models import AudioFile
from ..platform import dataset_catalog

ORIGINAL = re.compile(r"^([0-9a-f]{64})(\.[A-Za-z0-9]+)?$")
MOVED = "originals.moved"


def _move_folder(src: Path, dest: Path) -> None:
    """src renamed dest; into it, file by file, when dest is there from an earlier run (never overwriting)."""
    if not dest.exists():
        src.rename(dest)
        return
    for p in sorted(src.iterdir()):
        target = dest / p.name
        if target.exists():
            target = dest / f"{p.name}.{len(list(dest.glob(p.name + '*')))}"
        p.rename(target)
    src.rmdir()


def move_originals(session: Session, log: Callable[[str], None] = print) -> dict[str, Any]:
    if not dataset_catalog.enabled():
        raise dataset_catalog.Unavailable("the Dataset Catalog is not set up: set DATASET_STORE to the store's mount")
    root = paths.data_root() / "originals"
    files = sorted(p for p in root.iterdir() if p.is_file()) if root.is_dir() else []
    used = set(session.scalars(select(AudioFile.sha256).distinct()))
    stored: list[str] = []
    out: dict[str, Any] = {"files": len(files), "uploaded": 0, "already_in_store": 0, "held": 0, "not_used": 0,
                           "skipped": [], "failed": [], "moved_to": None}
    for k, p in enumerate(files, 1):
        m = ORIGINAL.match(p.name)
        if m is None:
            out["skipped"].append(p.name)  # not an original Bench wrote; it moves with the folder
            continue
        sha = m.group(1)
        if paths.store_audio(sha) is not None:
            out["already_in_store"] += 1
        else:
            try:
                dataset_catalog.upload(p, sha)
            except dataset_catalog.DatasetCatalogError as exc:
                out["failed"].append(f"{p.name}: {dataset_catalog.message(exc)}")
                continue
            out["uploaded"] += 1
        stored.append(sha)
        if k % 100 == 0 or k == len(files):
            log(f"{k}/{len(files)} originals checked: {out['uploaded']} uploaded, {out['already_in_store']} already "
                "in the store")
    held = [s for s in stored if s in used]
    dataset_catalog.hold(held)
    out["held"], out["not_used"] = len(held), len(stored) - len(held)
    if out["failed"]:
        log(f"{len(out['failed'])} file(s) failed, so {root} stays where it is:")
        for line in out["failed"][:50]:
            log(f"  {line}")
        return out
    if root.is_dir():
        _move_folder(root, root.with_name(MOVED))
        out["moved_to"] = str(root.with_name(MOVED))
        log(f"{root} is now {out['moved_to']}: delete it when you are satisfied that the store has everything")
    return out
