"""The inference cache (`model_output`) and the model catalog's output index.

An output is keyed on (model, build key, audio sha256, span, params hash): the build key is the exact
build the catalog reports for the model (image + config + weights), and the params hash is
`aicore.catalog.params_hash` of the adapter params, which Concord Label computes the same way. A lookup
goes:

1. a local row with that key;
2. the catalog's output index (`index_lookup`), asked once for a whole file or stage (`prefetch`): a hit
   another app holds is read from that app (POST {owner_url}/api/outputs/batch) and copied into
   `model_output`, keeping the owner and its output id, then recorded in the index as Bench's too;
3. otherwise the model is called through the catalog's gateway, and the row is stored and recorded in
   the index (`flush`, once per file).

An output is never indexed without a build key. Rows from before the catalog have none: they stay
readable by id (old runs score) and never match a lookup. Spans are stored the way the index stores
them: rounded to 3 decimals, (-1, -1) for the whole file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import numpy as np
from aicore.catalog import index_key
from aicore.catalog import span as index_span
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..adapters.base import Adapter, AudioRef
from ..core.models import ModelOutput
from ..platform import catalog

WHOLE = -1.0
PEER_CHUNK = 200  # output ids per read from another app

Key = tuple[str, float, float]  # (audio sha256, span_start, span_end)


def span(start: float | None, end: float | None) -> tuple[float, float]:
    return index_span(start, end)


def key(sha256: str, start: float | None, end: float | None) -> Key:
    return (sha256, *span(start, end))


def lookup(session: Session, service: str, build_key: str | None, sha256: str, start: float, end: float,
           phash: str) -> ModelOutput | None:
    if not build_key:
        return None
    s, e = span(start, end)
    return session.scalar(select(ModelOutput).where(
        ModelOutput.model_name == service, ModelOutput.build_key == build_key, ModelOutput.audio_sha256 == sha256,
        ModelOutput.span_start == s, ModelOutput.span_end == e, ModelOutput.params_hash == phash))


def _expected_shape(adapter: Adapter, output: Any) -> bool:
    if not isinstance(output, dict):
        return False
    return isinstance(output.get("segments"), list) if adapter.kind == "seg" else isinstance(output.get("text"), str)


def _id(x: str) -> Any:
    return int(x) if x.isdigit() else x


class OutputCache:
    """Lookups, copies from other apps, inference and index records for one job (one session)."""

    def __init__(self, session: Session, log: Callable[[str], None] | None = None):
        self.session = session
        self.log = log or (lambda m: None)
        self._pending: list[dict[str, Any]] = []
        self._index_down = False

    # ------------------------------------------------------------ local

    def lookup_many(self, adapter: Adapter, build_key: str | None, keys: Iterable[Key]) -> dict[Key, ModelOutput]:
        keys = set(keys)
        if not build_key or not keys:
            return {}
        shas = sorted({k[0] for k in keys})
        out: dict[Key, ModelOutput] = {}
        for i in range(0, len(shas), 500):
            rows = self.session.scalars(select(ModelOutput).where(
                ModelOutput.model_name == adapter.service, ModelOutput.build_key == build_key,
                ModelOutput.params_hash == adapter.params_hash, ModelOutput.audio_sha256.in_(shas[i:i + 500])))
            for r in rows:
                k = (r.audio_sha256, r.span_start, r.span_end)
                if k in keys:
                    out[k] = r
        return out

    # ------------------------------------------------------------ the catalog's index

    def _index_failed(self, what: str, exc: Exception) -> None:
        if not self._index_down:
            self.log(f"output index unavailable ({what}: {exc}); outputs are computed and kept here only")
        self._index_down = True

    def prefetch(self, adapter: Adapter, build_key: str | None, keys: Iterable[Key]) -> dict[str, int]:
        """Ask the index once for every key with no local row, and copy the hits other apps hold.
        Returns counts: local, copied, missing."""
        keys = list(dict.fromkeys(keys))
        local = self.lookup_many(adapter, build_key, keys)
        missing = [k for k in keys if k not in local]
        counts = {"local": len(local), "copied": 0, "missing": len(missing)}
        if not build_key or not missing or self._index_down:
            return counts
        phash = adapter.params_hash
        try:
            hits = catalog.get().index_lookup(catalog.APP, [index_key(build_key, sha, s, e, phash)
                                                            for sha, s, e in missing])
        except Exception as exc:
            if not catalog.unavailable(exc):
                raise
            self._index_failed("lookup", exc)
            return counts
        by_owner: dict[tuple[str, str], dict[str, Key]] = {}
        for k, hit in zip(missing, hits, strict=True):
            # Bench's own entries with no local row are stale (an earlier database): computed again.
            if hit and hit.get("owner") != catalog.APP and hit.get("owner_url"):
                by_owner.setdefault((hit["owner"], hit["owner_url"]), {})[str(hit["output_id"])] = k
        for (owner, url), wanted in by_owner.items():
            ids = list(wanted)
            for i in range(0, len(ids), PEER_CHUNK):
                try:
                    got = catalog.read_outputs(url, [_id(x) for x in ids[i:i + PEER_CHUNK]])
                except Exception as exc:  # noqa: BLE001 - the owner is down or changed: compute here instead
                    self.log(f"could not read outputs from {owner} at {url}: {exc}")
                    break
                for out in got:
                    k = wanted.get(str(out.get("id")))
                    if k is not None and self._copy(adapter, build_key, k, owner, out):
                        counts["copied"] += 1
        counts["missing"] -= counts["copied"]
        if counts["copied"]:
            self.log(f"{adapter.service}: {counts['copied']} output(s) read from other apps through the index")
        return counts

    def _copy(self, adapter: Adapter, build_key: str, k: Key, owner: str, out: dict[str, Any]) -> bool:
        sha, s, e = k
        same = (out.get("build_key") == build_key and out.get("audio_sha256") == sha
                and span(out.get("span_start"), out.get("span_end")) == (s, e)
                and out.get("params_hash") == adapter.params_hash and _expected_shape(adapter, out.get("output")))
        if not same:
            return False
        row = ModelOutput(model_name=adapter.service, build_key=build_key, audio_sha256=sha, span_start=s, span_end=e,
                          params_hash=adapter.params_hash, params=out.get("params") or adapter.params,
                          output=out["output"], latency_s=float(out.get("latency_s") or 0.0), source_app=owner,
                          source_output_id=str(out.get("id")))
        return self.store(row) is row

    def store(self, row: ModelOutput) -> ModelOutput:
        """Insert a row (savepoint), queueing its index record; after a lost race, the row that won."""
        try:
            with self.session.begin_nested():  # savepoint: losing a race must not undo the caller's work
                self.session.add(row)
        except IntegrityError:  # another process stored it first
            other = lookup(self.session, row.model_name, row.build_key, row.audio_sha256, row.span_start,
                           row.span_end, row.params_hash)
            assert other is not None
            return other
        if row.build_key:
            self._pending.append({**index_key(row.build_key, row.audio_sha256, row.span_start, row.span_end,
                                              row.params_hash), "output_id": row.id})
        return row

    def flush(self) -> int:
        """Record the stored outputs in the index; call it after committing them. Failures are logged."""
        pending, self._pending = self._pending, []
        if not pending or self._index_down:
            return 0
        try:
            return catalog.get().index_record(catalog.APP, pending)
        except Exception as exc:
            if not catalog.unavailable(exc):
                raise
            self._index_failed("record", exc)
            return 0

    # ------------------------------------------------------------ inference

    def run(self, adapter: Adapter, audio: np.ndarray, sha256: str, start: float, end: float,
            build_key: str | None, image: str | None = None) -> tuple[ModelOutput, bool]:
        """(row, hit?) for one call; `audio` is the exact samples to send (already cut for a span). Only
        a local row is a hit here: `prefetch` first to bring in what other apps have."""
        s, e = span(start, end)
        row = lookup(self.session, adapter.service, build_key, sha256, s, e, adapter.params_hash)
        if row is not None:
            return row, True
        output, latency = adapter.run(audio, AudioRef(sha256, s, e))
        row = new_row(adapter, (sha256, s, e), output, latency, build_key, image)
        stored = self.store(row)
        return stored, stored is not row

    @property
    def pending(self) -> int:
        """Stored rows not yet recorded in the index."""
        return len(self._pending)


def new_row(adapter: Adapter, k: Key, output: dict[str, Any], latency: float, build_key: str | None,
            image: str | None) -> ModelOutput:
    sha, s, e = k
    return ModelOutput(model_name=adapter.service, build_key=build_key, audio_sha256=sha, span_start=s, span_end=e,
                       params_hash=adapter.params_hash, params=adapter.params, output=output, latency_s=latency,
                       image=image)


def get_or_run(session: Session, adapter: Adapter, audio: np.ndarray, sha256: str, start: float = WHOLE,
               end: float = WHOLE, build_key: str | None = None, image: str | None = None) -> tuple[ModelOutput, bool]:
    """One call through the whole chain (local, index, other apps, inference). Jobs batch with OutputCache."""
    oc = OutputCache(session)
    pre = oc.prefetch(adapter, build_key, [key(sha256, start, end)])
    row, hit = oc.run(adapter, audio, sha256, start, end, build_key, image)
    session.flush()
    oc.flush()
    return row, hit or bool(pre["copied"])


def outputs_for(session: Session, ids: list[int]) -> dict[int, ModelOutput]:
    if not ids:
        return {}
    return {r.id: r for r in session.scalars(select(ModelOutput).where(ModelOutput.id.in_(ids)))}


def as_dict(row: ModelOutput) -> dict[str, Any]:
    return {"id": row.id, "model": row.model_name, "span": [row.span_start, row.span_end], "output": row.output,
            "latency_s": row.latency_s, "build_key": row.build_key, "source_app": row.source_app}


def served(row: ModelOutput) -> dict[str, Any]:
    """A row as other apps read it (POST /api/outputs/batch, platform/catalog/README.md)."""
    return {"id": row.id, "build_key": row.build_key, "audio_sha256": row.audio_sha256, "span_start": row.span_start,
            "span_end": row.span_end, "params_hash": row.params_hash, "params": row.params, "output": row.output,
            "latency_s": row.latency_s}
