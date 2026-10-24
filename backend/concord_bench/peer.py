"""Concord Label, the peer app (CONTRACTS.md §1, §3, §5; SPLIT_PLAN.md §5).

`LABEL_URL` names it. `status()` asks its `/api/config` with a short timeout and keeps the answer for
30 s, so pages that show or hide Label links stay fast when Label is down. Bench calls Label, or links
to it, only when it answered as `concord-label` with the contract version Bench implements.

Links and flags name a dataset by its Dataset Catalog id (contract version 3): they exist for a Bench dataset
imported from a catalog version or published as one (`Dataset.catalog_dataset_id`), and Label finds its working
dataset by that id.
"""

from __future__ import annotations

import contextlib
import threading
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import CONTRACT_VERSION
from .core.models import AudioFile, Dataset, Segment
from .core.settings import get_settings

PEER_APP = "concord-label"
TIMEOUT_S = 1.0
CACHE_S = 30.0
PUSH_TIMEOUT_S = 15.0

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


class PeerError(Exception):
    """Label refused or could not be reached; the message is shown to the user."""


def url() -> str | None:
    u = (get_settings().label_url or "").strip().rstrip("/")
    return u or None


def status(refresh: bool = False) -> dict[str, Any]:
    """`peer` of GET /api/config: {"url", "reachable", "contract_version"}."""
    base = url()
    if base is None:
        return {"url": None, "reachable": False, "contract_version": None}
    now = time.monotonic()
    with _lock:
        hit = _cache.get(base)
        if hit and not refresh and now - hit[0] < CACHE_S:
            return dict(hit[1])
    out: dict[str, Any] = {"url": base, "reachable": False, "contract_version": None}
    try:
        r = httpx.get(f"{base}/api/config", timeout=TIMEOUT_S)
        body = r.json() if r.status_code == 200 else {}
        if isinstance(body, dict) and body.get("app") == PEER_APP:
            out["reachable"] = True
            out["contract_version"] = body.get("contract_version")
    except (httpx.HTTPError, ValueError):
        pass
    with _lock:
        _cache[base] = (now, out)
    return dict(out)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def linked() -> bool:
    s = status()
    return bool(s["reachable"]) and s["contract_version"] == CONTRACT_VERSION


def file_link(ds: Dataset, sha256: str, t: float = 0.0) -> str | None:
    """`LABEL_URL/label/file/by-sha/<sha256>?catalog_dataset_id=<id>&t=<start>`, or None when the dataset has no
    catalog dataset or Label is not linked (CONTRACTS.md §5)."""
    if not ds.catalog_dataset_id or not linked():
        return None
    return (f"{url()}/label/file/by-sha/{sha256}?catalog_dataset_id={quote(ds.catalog_dataset_id)}"
            f"&t={round(max(t, 0.0), 3)}")


def file_link_for(session: Session, audio_file_id: int, t: float = 0.0) -> str | None:
    if url() is None:
        return None
    f = session.get(AudioFile, audio_file_id)
    return file_link(f.dataset, f.sha256, t) if f is not None else None


# ---------------------------------------------------------------- flags back to Label (CONTRACTS.md §3)

def flags_file(session: Session, ds: Dataset) -> dict[str, Any]:
    """The dataset's flagged references as a flags file (format version 2), named by the dataset's catalog
    dataset and version. The uid is the sender's segment uid when the version carried one (refs/uids.jsonl),
    else null."""
    if not ds.catalog_dataset_id or not ds.catalog_version:
        raise PeerError("this dataset is not a Dataset Catalog version, so its flags cannot go back to Concord "
                        "Label")
    src = (ds.provenance or {}).get("source") or {}
    rows = session.execute(select(Segment, AudioFile).join(AudioFile)
                           .where(AudioFile.dataset_id == ds.id, Segment.flagged.is_(True))
                           .order_by(AudioFile.rel_path, Segment.start)).all()
    return {
        "format": "concord-flags",
        "format_version": 2,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "catalog_dataset_id": ds.catalog_dataset_id,
        "catalog_version": ds.catalog_version,
        "dataset_name": src.get("dataset_name") or ds.name,
        "flags": [{"uid": seg.uid if (seg.meta or {}).get("source_uid") else None, "file_sha256": f.sha256,
                   "start": seg.start, "end": seg.end, "note": seg.flag_note} for seg, f in rows],
    }


def push_flags(doc: dict[str, Any]) -> dict[str, Any]:
    """POST the flags file to Label: {"matched", "unmatched"}."""
    if not linked():
        s = status()
        if s["url"] is None:
            why = "LABEL_URL is not set"
        elif not s["reachable"]:
            why = f"Concord Label at {s['url']} did not answer"
        else:
            why = f"Concord Label implements contract version {s['contract_version']}, Bench {CONTRACT_VERSION}"
        raise PeerError(f"cannot send flags: {why}. Download the flags file instead.")
    try:
        r = httpx.post(f"{url()}/api/flags/import", json=doc, timeout=PUSH_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise PeerError(f"Concord Label did not answer: {type(exc).__name__}") from None
    if r.status_code == 404:
        raise PeerError(f"Concord Label has no dataset made from or published to {doc['dataset_name']!r} "
                        f"(catalog dataset {doc['catalog_dataset_id']})")
    if r.status_code >= 400:
        detail: Any = r.text[:300]
        with contextlib.suppress(ValueError, AttributeError):
            detail = r.json().get("detail", detail)
        raise PeerError(f"Concord Label refused the flags ({r.status_code}): {detail}")
    body = r.json()
    return {"matched": int(body.get("matched", 0)), "unmatched": int(body.get("unmatched", 0))}
