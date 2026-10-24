"""Bench's side of the Dataset Catalog's datasets and versions (DATASET_CATALOG_PLAN.md §9.2, §9.3).

- `datasets` / `dataset` list the catalog's datasets and versions for "Import references", marking which
  versions Bench can import (they have references) and which Bench datasets are made from each one.
- `stage_version` stages a version as a reference import, read from its folder on the store's mount, so
  the import preview and accept are the ones a zip gets (`refpackage`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import paths
from ..core.models import Dataset, ReferenceImport
from ..platform import dataset_catalog
from .refpackage import PackageError, stage

# The NO_REFERENCES rule of an upload (datasets.ingest), for a version with audio only (format version 3).
NO_REFERENCES_VERSION = ("v{n} has audio only: Concord Bench benchmarks datasets with references. Label it in "
                         "Concord Label and publish a version with references, then import that one.")


def importable(version: dict[str, Any] | None) -> bool:
    """A version with references (an audio-only version has no label types)."""
    return bool(version and version.get("label_types"))


def _bench(session: Session, catalog_id: str) -> dict[int, list[dict[str, Any]]]:
    """The Bench datasets made from or published as each version of a catalog dataset, by version number."""
    out: dict[int, list[dict[str, Any]]] = {}
    for d in session.scalars(select(Dataset).where(Dataset.catalog_dataset_id == catalog_id).order_by(Dataset.id)):
        out.setdefault(d.catalog_version or 0, []).append(
            {"id": d.id, "name": d.name, "status": d.status, "published": not dataset_catalog.from_catalog(d)})
    return out


def _version(v: dict[str, Any], bench: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    keep = ("dataset_id", "n", "parent_n", "parent_deleted", "label_types", "labels", "counts", "published_by",
            "published_at", "format_version")
    return {**{k: v.get(k) for k in keep}, "importable": importable(v), "bench": bench.get(v["n"], [])}


def datasets(q: str | None = None) -> list[dict[str, Any]]:
    """The catalog's datasets, most recently changed first, each with its latest version."""
    with dataset_catalog.calling() as dc:
        rows = dc.datasets(q=q or None)
    return [{**{k: d.get(k) for k in ("id", "name", "language", "description", "tags", "created_by", "updated_at")},
             "latest": _version(d["latest"], {}) if d.get("latest") else None} for d in rows]


def dataset(session: Session, catalog_id: str) -> dict[str, Any]:
    """One catalog dataset with its versions (newest first) and the Bench datasets made from each."""
    with dataset_catalog.calling() as dc:
        d = dc.dataset(catalog_id)
    bench = _bench(session, catalog_id)
    return {**{k: d.get(k) for k in ("id", "name", "language", "description", "tags", "created_by", "updated_at")},
            "latest": _version(d["latest"], bench) if d.get("latest") else None,
            "versions": [_version(v, bench) for v in d.get("versions") or []],
            "url": f"{dataset_catalog.ui_url()}/{catalog_id}"}


def stage_version(session: Session, catalog_id: str, n: int) -> ReferenceImport:
    """A catalog version staged for import. An import of the same version that is still staged is returned
    instead of a second one."""
    for imp in session.scalars(select(ReferenceImport).where(ReferenceImport.status == "staged")):
        cat = (imp.preview or {}).get("catalog") or {}
        if cat.get("dataset_id") == catalog_id and cat.get("n") == n:
            return imp
    with dataset_catalog.calling() as dc:
        d = dc.dataset(catalog_id)
        v = dc.version(catalog_id, n)
    if not importable(v):
        raise PackageError(NO_REFERENCES_VERSION.format(n=n))
    folder = paths.store_version(catalog_id, n)
    if folder is None or not (folder / "manifest.json").is_file():
        raise PackageError(f"version {n} is not at {folder} on the store's mount: DATASET_STORE must be where the "
                           "Dataset Catalog's store is mounted here")
    return stage(session, None, folder, catalog={"dataset_id": catalog_id, "n": n, "dataset_name": d["name"]})
