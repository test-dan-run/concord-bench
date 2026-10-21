"""Model adapters: one per (contract, api), built on `aicore.client(name)`."""

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
    """Adapter for a registered `Model`. The image tag recorded at registration is replaced by the
    current one from the endpoint map, and both the tag and the timing level go into the cache key."""
    from ..platform.services import image_info

    snap = model.registry_snapshot or {}
    cls = adapter_class(snap.get("contract", ""), snap.get("api", ""))
    if cls is None:
        raise AdapterError(f"{model.registry_name}: no adapter for {snap.get('contract')} / {snap.get('api')}")
    image = image_info(model.registry_name).get("image") or snap.get("image")
    options: dict[str, Any] = {"image": image}
    if cls.kind == "asr":
        options["timing"] = model.timing_level or "text"
        if cls.api == "audio.transcriptions" and whisper_language(language):
            options["language"] = whisper_language(language)
    return cls(model.registry_name, options)
