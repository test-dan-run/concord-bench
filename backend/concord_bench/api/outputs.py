"""Outputs other apps read (platform/catalog/README.md, "What every app serves"): the catalog's output
index points Concord Label here when Bench holds an output it would otherwise compute again."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_session
from ..core.models import ModelOutput
from ..runs.cache import served

router = APIRouter(tags=["outputs"])


class BatchIn(BaseModel):
    ids: list[int | str] = Field(max_length=5000)


@router.post("/outputs/batch")
def outputs_batch(body: BatchIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """`{"ids": [812, 813]}` -> `{"outputs": [...]}` in the order asked; missing ids are left out."""
    ids = [int(i) for i in body.ids if isinstance(i, int) or str(i).isdigit()]
    rows = {r.id: r for r in session.scalars(select(ModelOutput).where(ModelOutput.id.in_(ids)))} if ids else {}
    return {"outputs": [served(rows[i]) for i in dict.fromkeys(ids) if i in rows]}
