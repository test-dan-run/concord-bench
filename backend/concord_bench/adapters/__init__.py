"""Model adapters: one per (contract, api), built on `aicore.client(name)` (the catalog's gateway)."""

from __future__ import annotations

from typing import Any

from .base import Adapter, AdapterError, AudioRef, ReplayMiss, adapter_class

__all__ = ["Adapter", "AdapterError", "AudioRef", "ReplayMiss", "adapter_class", "for_model"]


def whisper_language(language: str | None) -> str | None:
    """`en-SG` -> `en`; free text such as `English` is not a code, so it is not sent."""
    if not language:
        return None
    code = language.split("-")[0].split("_")[0].lower()
    return code if 2 <= len(code) <= 3 and code.isalpha() else None


def for_model(model: Any, language: str | None = None) -> Adapter:
    """Adapter for a catalog `Model`. Contract, api and timing level come from the catalog's record of the
    model's current build. The build itself is not an adapter option: it is part of the cache key on its
    own (`runs.cache`), so params hash the same in Bench and Label for every build."""
    rec = model.catalog or {}
    if getattr(model, "is_enh", False):
        from .enhance import OIPEnhancer

        return OIPEnhancer(model.catalog_id)
    cls = adapter_class(rec.get("contract", ""), rec.get("api", ""))
    if cls is None:
        raise AdapterError(f"{model.catalog_id}: no adapter for {rec.get('contract')} / {rec.get('api')}")
    options: dict[str, Any] = {}
    if cls.kind == "asr":
        options["timing"] = model.timing_level or "text"
        if cls.api == "audio.transcriptions" and whisper_language(language):
            options["language"] = whisper_language(language)
    return cls(model.catalog_id, options)
