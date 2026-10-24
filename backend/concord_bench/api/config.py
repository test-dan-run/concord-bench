"""Config (app, version, contract version, peer), tags, normalisers, task list."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import APP, CONTRACT_VERSION, __version__, peer
from ..core.db import get_session
from ..core.models import Dataset, Model
from ..core.settings import get_settings
from ..metrics import normalisers
from ..metrics.der import DEFAULT_COLLAR

router = APIRouter(tags=["config"])

# Benchmark tabs are built from this list, so KWS is one more entry when it lands (Q6).
TASKS: list[dict[str, Any]] = [
    {"id": "asr", "label": "ASR", "label_type": "asr", "metric": "WER", "enabled": True},
    {"id": "diar", "label": "Diarisation", "label_type": "diar", "metric": "DER", "enabled": True},
    {"id": "pipeline", "label": "ASR + diarisation pipelines", "label_type": "asr", "metric": "cpWER",
     "enabled": True},
]


@router.get("/config")
def get_config() -> dict[str, Any]:
    s = get_settings()
    return {
        "app": APP,
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
        # Concord Label at LABEL_URL (CONTRACTS.md §1): links and pushes need reachable + the same contract version.
        "peer": peer.status(),
        "tasks": TASKS,
        "default_normaliser": normalisers.DEFAULT,
        "default_collar": DEFAULT_COLLAR,
        "collars": [0.0, 0.1, 0.25, 0.5],
        "replay": s.replay_dir is not None,
        # The model catalog: models, leases and the gateway; `ui_url` is where models are registered.
        "catalog": {"url": s.catalog_url.rstrip("/"), "ui_url": s.catalog_ui},
        # The Dataset Catalog (DATASET_CATALOG_PLAN.md §9): set up when DATASET_STORE is set. The pages show
        # import from the catalog and publishing only then.
        "dataset_catalog": {"enabled": s.dataset_store is not None, "ui_url": s.dataset_catalog_ui},
        "links": {"platform_readme": "dev/README.md", "contracts_readme": "platform/contracts/README.md"},
    }


def text_transform_services() -> list[str]:
    """Platform `text.transform` services the catalog knows (called through its gateway)."""
    from ..platform import catalog

    try:
        return sorted(r["id"] for r in catalog.records().values() if r.get("api") == "text.transform")
    except catalog.CatalogUnavailable:
        return []


@router.get("/normalisers")
def list_normalisers() -> list[dict[str, Any]]:
    out = [{"id": n.id, "key": n.key, "label": n.label, "description": n.description, "default": n.id ==
            normalisers.DEFAULT, "service": None} for n in normalisers.BUILTIN.values()]
    for svc in text_transform_services():
        n = normalisers.platform_normaliser(svc)
        out.append({"id": n.id, "key": n.key, "label": n.label, "description": n.description, "default": False,
                    "service": svc})
    return out


@router.get("/tags")
def list_tags(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Every tag used on datasets and models, most used first, for the tag inputs' suggestions."""
    counts: dict[str, int] = {}
    for tags in session.scalars(select(Dataset.tags)):
        for t in tags or []:
            counts[t] = counts.get(t, 0) + 1
    for tags in session.scalars(select(Model.tags)):
        for t in tags or []:
            counts[t] = counts.get(t, 0) + 1
    return [{"tag": t, "count": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
