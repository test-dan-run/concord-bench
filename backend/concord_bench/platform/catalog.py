"""The model catalog as Concord Bench uses it (platform/catalog/README.md), through `aicore.catalog`.

Bench registers, verifies, starts and calls no model service itself:
- model records (state, capabilities of the current build, cold start, build key) come from one
  `GET /api/models?all=1`, kept for RECORDS_TTL_S, so pages never ask the catalog once per model;
- a job leases the models it needs (`request_lease`; the aicore client heartbeats the lease);
- inference goes through the catalog's gateway: `export_platform_env` sets AICORE_CATALOG, so
  `aicore.client(name)` points there;
- `runs.cache` looks outputs up in the catalog's output index and records its own there, and reads
  another app's outputs with `read_outputs` (POST {owner_url}/api/outputs/batch).

A `Model` row is made on a model's first use (`ensure_model`) and keeps the last record seen.

Replay mode (REPLAY_DIR) needs no catalog: `get()` returns a `FakeCatalog` holding the services of the
recorded endpoint map, with the capabilities recorded in summary.json and a build key derived from each
image tag, so replayed runs have stable cache keys. Tests install their own with `use()`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from aicore.catalog import Catalog, CatalogError, FakeCatalog, Lease
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.models import Model
from ..core.settings import get_settings

log = logging.getLogger("concord_bench.catalog")

APP = "bench"  # this app's name in the catalog (leases are held by "bench:job-<id>")
RECORDS_TTL_S = 10.0
PEER_TIMEOUT_S = 60.0
# Record fields kept on a Model row (`Model.catalog`): what changes outputs or what pages show, not state.
SNAPSHOT_KEYS = ("contract", "api", "kind", "engine", "description", "image", "build_key", "parent_id")


class CatalogUnavailable(Exception):
    """The catalog did not answer (or answered with an error)."""


class ModelUnusable(Exception):
    """A model a job needs is unknown to the catalog, unregistered, or not verified for its current build."""


# ---------------------------------------------------------------- the client

_override: Any = None
_records: tuple[float, dict[str, dict[str, Any]]] | None = None
_lock = threading.Lock()


def get() -> Catalog | FakeCatalog:
    """The catalog client: a test's stand-in, the replay stand-in, or the real catalog at catalog_url."""
    if _override is not None:
        return _override
    s = get_settings()
    if s.replay_dir is not None:
        return _replay(str(s.replay_dir))
    return _real(s.catalog_url)


@lru_cache(maxsize=4)
def _real(url: str) -> Catalog:
    return Catalog(url, timeout=15.0)


@lru_cache(maxsize=4)
def _replay(root: str) -> FakeCatalog:
    return replay_catalog(Path(root))


def use(catalog: Any) -> None:
    """Tests: answer with this catalog (None: back to the default)."""
    global _override
    reset()
    _override = catalog


def reset() -> None:
    global _override, _records
    _override, _records = None, None
    _real.cache_clear()
    _replay.cache_clear()


def unavailable(exc: Exception) -> bool:
    return isinstance(exc, (CatalogError, httpx.HTTPError, OSError))


# ---------------------------------------------------------------- model records

def records(refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Every platform service the catalog knows (registered or not), by model id. Cached for RECORDS_TTL_S."""
    global _records
    with _lock:
        if not refresh and _records is not None and time.monotonic() - _records[0] < RECORDS_TTL_S:
            return _records[1]
    try:
        recs = {r["id"]: r for r in get().models(all=True)}
    except Exception as exc:
        if unavailable(exc):
            raise CatalogUnavailable(f"model catalog at {get().url} not reachable: {exc}") from None
        raise
    with _lock:
        _records = (time.monotonic(), recs)
    return recs


def record(model_id: str, refresh: bool = False) -> dict[str, Any] | None:
    """One model's record by id or alias, from the cached listing."""
    recs = records(refresh)
    if model_id in recs:
        return recs[model_id]
    return next((r for r in recs.values() if model_id in (r.get("aliases") or [])), None)


def tasks_of(rec: dict[str, Any]) -> list[str]:
    """Tasks the verifier observed for the current build: asr, vad, diar."""
    return list((rec.get("capabilities") or {}).get("tasks") or [])


def stages_of(rec: dict[str, Any]) -> list[str]:
    t = set(tasks_of(rec))
    return (["seg"] if t & {"vad", "diar"} else []) + (["asr"] if "asr" in t else [])


def speech_model(rec: dict[str, Any]) -> bool:
    """A registered model Bench can benchmark: verified (or awaiting verification) as ASR or a segmenter."""
    caps = rec.get("capabilities") or {}
    return bool(rec.get("registered", True)) and (bool(stages_of(rec)) or caps.get("usable_as") in
                                                  ("asr", "chat-audio", "segmenter"))


def unusable_reason(rec: dict[str, Any] | None, model_id: str = "") -> str | None:
    """Why a job cannot lease this model, or None when it can."""
    if rec is None:
        return f"{model_id} is not in the model catalog"
    if not rec.get("registered", True):
        return f"{rec['id']} is not registered in the model catalog"
    caps = rec.get("capabilities")
    if not caps:
        return f"{rec['id']} has not been verified by the model catalog yet"
    status = caps.get("status")
    if status == "failed":
        lines = (caps.get("report") or {}).get("lines") or []
        why = "; ".join(f"{ln.get('name')}: {ln.get('reason')}" for ln in lines if ln.get("status") == "fail")
        return f"{rec['id']} failed verification in the model catalog" + (f" ({why})" if why else "")
    if status != "verified":
        return f"{rec['id']} is not verified for its current build yet (capabilities {status})"
    return None


def snapshot(rec: dict[str, Any]) -> dict[str, Any]:
    snap = {k: rec[k] for k in SNAPSHOT_KEYS if rec.get(k) is not None}
    caps = rec.get("capabilities")
    if caps:
        snap["capabilities"] = {k: v for k, v in caps.items() if k != "report"}
    return snap


def _apply(session: Session, m: Model, rec: dict[str, Any]) -> None:
    snap = snapshot(rec)
    if m.catalog != snap:
        m.catalog = snap
    name = rec.get("display_name") or rec["id"]
    if m.display_name != name:
        m.display_name = name
    tags = list(rec.get("tags") or [])
    if m.tags != tags:
        m.tags = tags
    parent = rec.get("parent_id")
    pid = session.scalar(select(Model.id).where(Model.catalog_id == parent)) if parent else None
    if m.parent_model_id != pid:
        m.parent_model_id = pid


def ensure_model(session: Session, model_id: str, rec: dict[str, Any] | None = None) -> Model:
    """The Model row for a catalog model, made on first use and refreshed from its record.
    Raises ModelUnusable for a model the catalog does not know."""
    rec = rec or record(model_id)
    if rec is None:
        existing = session.scalar(select(Model).where(Model.catalog_id == model_id))
        if existing is not None:
            return existing
        raise ModelUnusable(f"{model_id} is not in the model catalog")
    m = session.scalar(select(Model).where(Model.catalog_id == rec["id"]))
    if m is None:
        m = Model(catalog_id=rec["id"], display_name=rec.get("display_name") or rec["id"])
        session.add(m)
    _apply(session, m, rec)
    session.flush()
    return m


def sync_models(session: Session, recs: dict[str, dict[str, Any]]) -> None:
    """Refresh the rows Bench already has from these records (no new rows). Writes only what changed."""
    for m in session.scalars(select(Model)):
        rec = recs.get(m.catalog_id)
        if rec is not None:
            _apply(session, m, rec)
    if session.dirty:
        session.commit()


# ---------------------------------------------------------------- leases, plans, apps

def request_lease(holder: str, models: list[str]) -> Lease:
    """Ask for a lease without waiting: the returned lease heartbeats until released, queued or not."""
    cat = get()
    return Lease(cat, cat.request_lease(holder, sorted(set(models))))


def plan(stages: list[dict[str, Any]], pipeline: str = "staged") -> dict[str, Any]:
    try:
        return get().plan(stages, pipeline)
    except Exception as exc:
        if unavailable(exc):
            raise CatalogUnavailable(f"model catalog: {exc}") from None
        raise


def register_app() -> None:
    """Tell the catalog where other apps read Bench's outputs. Tolerates a catalog that is down."""
    url = get_settings().public_url.rstrip("/")
    try:
        get().register_app(APP, url)
        log.info("registered with the model catalog as %s at %s", APP, url)
    except Exception as exc:  # noqa: BLE001 - startup must not depend on the catalog
        log.warning("could not register with the model catalog (%s); other apps cannot read Bench's outputs "
                    "until it is reachable and Bench restarts", exc)


def register_app_in_background() -> None:
    threading.Thread(target=register_app, name="catalog-register", daemon=True).start()


def read_outputs(owner_url: str, ids: list[Any]) -> list[dict[str, Any]]:
    """Another app's outputs by id (README "What every app serves"); missing ids are left out."""
    r = httpx.post(owner_url.rstrip("/") + "/api/outputs/batch", json={"ids": list(ids)}, timeout=PEER_TIMEOUT_S)
    r.raise_for_status()
    return list((r.json() or {}).get("outputs") or [])


# ---------------------------------------------------------------- replay

def _usable_as(e: dict[str, Any]) -> str | None:
    contract, api = e.get("contract"), e.get("api")
    if contract == "openai" and api == "audio.transcriptions":
        return "asr"
    if contract == "openai" and api == "chat.completions":
        return "chat-audio"
    inputs = {t.get("name") for t in (e.get("signature") or {}).get("inputs", [])}
    if contract == "oip-v2" and api == "infer" and {"AUDIO", "SAMPLE_RATE"} <= inputs:
        return "segmenter"
    return None


def replay_build_key(image: str | None) -> str:
    return hashlib.sha256(f"replay:{image}".encode()).hexdigest()[:16]


def replay_catalog(root: Path) -> FakeCatalog:
    """A stand-in catalog for recorded responses: the recording's endpoint map, every service running and
    registered, capabilities from summary.json (`models`, or `checks` in recordings from before the catalog)."""
    import yaml

    services = (yaml.safe_load((root / "endpoints.generated.yaml").read_text()) or {}).get("services") or {}
    try:
        summary = json.loads((root / "summary.json").read_text())
    except (OSError, ValueError):
        summary = {}
    caps = summary.get("models") or summary.get("checks") or {}
    recs: dict[str, dict[str, Any]] = {}
    for key, e in services.items():
        name = e["name"]
        rec = recs.get(name)
        if rec is None:
            bk = replay_build_key(e.get("image"))
            c = caps.get(name)
            rec = recs[name] = {
                "id": name, "aliases": [], "registered": True, "display_name": name, "tags": list(e.get("tasks") or []),
                "parent_id": None, "description": e.get("description"), "tasks": list(e.get("tasks") or []),
                "kind": e.get("kind"), "engine": e.get("engine"), "contract": e.get("contract"), "api": e.get("api"),
                "signature": e.get("signature"), "gpu": {"fraction": e.get("gpu_fraction") or 0.0},
                "state": "running", "state_detail": None, "leased_by": [], "in_flight": 0, "build_key": bk,
                "image": e.get("image"), "cold_start_s": 30.0, "cold_start_measured": False,
                "capabilities": {"build_key": bk, "status": "verified", "usable_as": _usable_as(e),
                                 "tasks": list(c["capabilities"]), "timing_level": c.get("timing_level")}
                if c else None,
            }
        if key != name:
            rec["aliases"].append(key)
    return FakeCatalog(list(recs.values()))
