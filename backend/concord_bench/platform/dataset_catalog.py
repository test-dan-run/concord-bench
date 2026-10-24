"""The Dataset Catalog as Concord Bench uses it (DATASET_CATALOG_PLAN.md §9, §12), through `aicore.datasets`.

The catalog service at CATALOG_URL serves both the Model Catalog and the Dataset Catalog. Bench uses the
Dataset Catalog only when DATASET_STORE is set (`enabled()`), and then:
- ingest uploads each original to the store (`upload`) and holds the dataset's audio (`hold`); Bench reads
  originals from the store's read-only mount (`core.paths.original`);
- deleting a dataset releases the holds that no other Bench dataset needs (`release_unused`);
- a version is imported from the mount (`datasets.catalog`), with a `use` naming the Bench dataset;
- a Bench dataset from an upload or a zip can be published as a version (`datasets.catalog`).

Tests install `aicore.datasets.FakeDatasetCatalog` with `use()`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

import httpx
from aicore.datasets import DatasetCatalog, DatasetCatalogError, NotFound, StaleParent
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import APP
from ..core import paths
from ..core.models import AudioFile, Dataset
from ..core.settings import get_settings

__all__ = ["APP", "INSTALL_ID", "DatasetCatalogError", "NotFound", "StaleParent", "Unavailable"]

# manifest.source.install_id of every catalog version, so Bench's source_key of a catalog dataset is
# "dataset-catalog/<dataset id>".
INSTALL_ID = "dataset-catalog"
SOURCE_PREFIX = INSTALL_ID + "/"


class Unavailable(Exception):
    """The Dataset Catalog is not set up here, or its service did not answer."""


_override: Any = None


def enabled() -> bool:
    """DATASET_STORE is set: the store is mounted and the Dataset Catalog is used."""
    return paths.store() is not None


def get() -> Any:
    """The client: a test's stand-in, or the catalog service at CATALOG_URL with the store at DATASET_STORE."""
    if _override is not None:
        return _override
    store = paths.store()
    if store is None:
        raise Unavailable("the Dataset Catalog is not set up: set DATASET_STORE to the store's mount")
    return _real(get_settings().catalog_url.rstrip("/"), str(store))


@lru_cache(maxsize=4)
def _real(url: str, store: str) -> DatasetCatalog:
    return DatasetCatalog(url, app=APP, store=store, timeout=120.0)


def use(client: Any) -> None:
    """Tests: answer with this client (None: back to the default)."""
    global _override
    reset()
    _override = client


def reset() -> None:
    global _override
    _override = None
    _real.cache_clear()


@contextmanager
def calling() -> Iterator[Any]:
    """The client, with a service that doesn't answer raised as Unavailable. The catalog's own refusals
    (NotFound, StaleParent, ...) come through as they are."""
    dc = get()
    try:
        yield dc
    except (httpx.HTTPError, OSError) as exc:
        raise Unavailable(f"the Dataset Catalog at {getattr(dc, 'url', '?')} did not answer: {exc}") from None


def message(exc: DatasetCatalogError) -> str:
    return exc.message if getattr(exc, "message", None) else str(exc)


# ---------------------------------------------------------------- audio and holds

def upload(path: Any, sha256: str) -> None:
    """Put an original in the store unless the mount already shows it (`upload_audio` asks the service too)."""
    if paths.store_audio(sha256) is not None:
        return
    with calling() as dc:
        dc.upload_audio(path, sha256)
    if paths.store_audio(sha256) is None:
        raise Unavailable(f"the Dataset Catalog stored {sha256}, but it is not under DATASET_STORE ({paths.store()}): "
                          "DATASET_STORE must be where the store is mounted here")


def hold(sha256s: Iterable[str]) -> None:
    shas = sorted(set(sha256s))
    if shas:
        with calling() as dc:
            dc.hold(shas)


def release_unused(session: Session, sha256s: Iterable[str], leaving: int) -> list[str]:
    """Release the holds on these files except the ones another Bench dataset (not `leaving`) still has."""
    shas = set(sha256s)
    if not shas:
        return []
    kept = set(session.scalars(select(AudioFile.sha256).where(AudioFile.sha256.in_(shas),
                                                              AudioFile.dataset_id != leaving)))
    free = sorted(shas - kept)
    if free:
        with calling() as dc:
            dc.release(free)
    return free


# ---------------------------------------------------------------- Bench datasets and catalog versions

def catalog_uid(source_key: str | None) -> str | None:
    """The catalog dataset id of a source key "dataset-catalog/<id>", else None."""
    return source_key[len(SOURCE_PREFIX):] if source_key and source_key.startswith(SOURCE_PREFIX) else None


def from_catalog(ds: Dataset) -> bool:
    """Made from a catalog version (imported from the catalog, or from a zip of one)."""
    return catalog_uid(ds.source_key) is not None

