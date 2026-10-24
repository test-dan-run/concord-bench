"""The Dataset Catalog from Bench's pages (DATASET_CATALOG_PLAN.md §9.2): its datasets and versions, and a
version staged as a reference import ("Import references", From Dataset Catalog). Accepting or rejecting it
goes through /api/reference-imports like a zip."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.db import get_session
from ..datasets import catalog, refpackage
from ..datasets.refpackage import PackageError
from ..platform import dataset_catalog

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
