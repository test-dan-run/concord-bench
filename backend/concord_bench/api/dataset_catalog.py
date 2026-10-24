"""The Dataset Catalog from Bench's pages (DATASET_CATALOG_PLAN.md §9.2, §9.3): its datasets and versions, a
version staged as a reference import ("Import references", From Dataset Catalog; accepting or rejecting it
goes through /api/reference-imports like a zip), and publishing a Bench dataset as a version."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.db import get_session
from ..core.models import Dataset
from ..datasets import catalog, refpackage
from ..datasets.catalog import PublishError
from ..datasets.refpackage import PackageError
from ..platform import dataset_catalog
from .datasets import dataset_json

router = APIRouter(tags=["dataset catalog"])


@contextmanager
def answering() -> Iterator[None]:
    """The catalog's answers as HTTP errors: not set up 409, no answer 503, not found 404, refused 422."""
    if not dataset_catalog.enabled():
        raise HTTPException(409, "the Dataset Catalog is not set up here (DATASET_STORE is not set)")
    try:
        yield
    except dataset_catalog.Unavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except dataset_catalog.NotFound as exc:
        raise HTTPException(404, dataset_catalog.message(exc)) from None
    except dataset_catalog.StaleParent as exc:
        raise HTTPException(409, stale_message(exc)) from None
    except dataset_catalog.DatasetCatalogError as exc:
        raise HTTPException(422 if exc.status < 500 else 502, dataset_catalog.message(exc)) from None


@router.get("/dataset-catalog/datasets")
def list_catalog_datasets(q: str | None = None) -> list[dict[str, Any]]:
    with answering():
        return catalog.datasets(q)


@router.get("/dataset-catalog/datasets/{catalog_id}")
def get_catalog_dataset(catalog_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    with answering():
        return catalog.dataset(session, catalog_id)


class VersionImportIn(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=64)
    n: int = Field(ge=1)


@router.post("/dataset-catalog/imports", status_code=201)
def stage_catalog_version(body: VersionImportIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """The version staged with the preview a zip gets; accept it at /api/reference-imports/{id}/accept."""
    with answering():
        try:
            imp = catalog.stage_version(session, body.dataset_id, body.n)
        except PackageError as exc:
            session.rollback()
            raise HTTPException(400, str(exc)) from None
    session.commit()
    return refpackage.import_json(imp)


def stale_message(exc: dataset_catalog.StaleParent) -> str:
    """What the person is asked before publishing anyway (DATASET_CATALOG_PLAN.md §5.3)."""
    v = exc.latest or {}
    if not v:
        return dataset_catalog.message(exc)
    when = str(v.get("published_at") or "")[:16].replace("T", " ")
    return (f"The latest version is now v{v.get('n')}, published by {v.get('published_by')} on {when} UTC, not the "
            "version this one is based on.")


class PublishIn(BaseModel):
    """No catalog_dataset_id: version 1 of a new catalog dataset named `name` (default: the dataset's name).
    With it: the next version, based on `parent`, the latest version the person saw (absent: whatever is latest
    now). `force` publishes on a stale parent after the person agreed."""
    catalog_dataset_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=200)
    parent: int | None = None
    force: bool = False


@router.post("/datasets/{dataset_id}/publish", status_code=201)
def publish_dataset(dataset_id: int, body: PublishIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """201 with the version (`created` false: the latest version had the same content). 409 only when the parent
    is stale, so the page asks whether to publish anyway; 400 when the dataset can't be published."""
    ds = session.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    with answering():
        try:
            v = catalog.publish(session, ds, catalog_id=body.catalog_dataset_id, name=(body.name or "").strip() or None,
                                parent=body.parent, latest="parent" not in body.model_fields_set, force=body.force)
        except PublishError as exc:
            session.rollback()
            raise HTTPException(400, str(exc)) from None
        except Exception:
            session.rollback()
            raise
    session.commit()
    return {"dataset": dataset_json(session, ds), "version": {k: v.get(k) for k in ("dataset_id", "n", "parent_n",
                                                                                     "label_types", "counts")},
            "created": v.get("created", True)}
