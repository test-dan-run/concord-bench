"""The platform registry as Concord sees it: `aicore.available()` deduplicated by service name
(aliases appear as separate keys), ranked by relevance, joined with registered models.

Nothing here is typed by the user: base URL, route, protocol, image tag and default tags (`tasks`)
all come from the endpoint map.
"""

from __future__ import annotations

from typing import Any

SPEECH_TASKS = ("speech-recognition", "voice-activity-detection", "speaker-diarization")


class RegistryUnavailable(Exception):
    pass


def usable_as(entry: dict[str, Any]) -> str | None:
    """What a probe could find: asr | chat-audio | segmenter | None (not usable as a model)."""
    contract, api = entry.get("contract"), entry.get("api")
    if contract == "openai" and api == "audio.transcriptions":
        return "asr"
    if contract == "openai" and api == "chat.completions":
        return "chat-audio"
    if contract == "oip-v2" and api == "infer":
        sig = entry.get("signature") or {}
        inputs = {t.get("name") for t in sig.get("inputs", [])}
        if {"AUDIO", "SAMPLE_RATE"} <= inputs:
            return "segmenter"
    return None


def relevance(entry: dict[str, Any]) -> int:
    """0 = trained for a speech task, 1 = audio-capable or might be, 2 = not usable."""
    if set(entry.get("tasks") or []) & set(SPEECH_TASKS) and usable_as(entry):
        return 0
    return 1 if usable_as(entry) else 2


def not_usable_reason(entry: dict[str, Any]) -> str | None:
    if usable_as(entry):
        return None
    if entry.get("api") == "text.transform":
        return "text.transform service: offered as a WER normaliser instead"
    if entry.get("contract") == "oip-v2":
        return "OIP signature does not take AUDIO + SAMPLE_RATE"
    return f"{entry.get('contract')} / {entry.get('api')} does not take audio for transcription or segmentation"


def entries() -> list[dict[str, Any]]:
    import aicore

    try:
        keys = aicore.available()
    except Exception as exc:  # noqa: BLE001 - missing endpoint map
        raise RegistryUnavailable(str(exc)) from None
    by_name: dict[str, dict[str, Any]] = {}
    for key in keys:
        ep = aicore.endpoint(key)
        e = by_name.setdefault(ep["name"], {**ep, "aliases": []})
        if key != ep["name"]:
            e["aliases"].append(key)
    out = []
    for e in by_name.values():
        e["usable_as"] = usable_as(e)
        e["relevance"] = relevance(e)
        e["not_usable"] = not_usable_reason(e)
        out.append(e)
    return sorted(out, key=lambda e: (e["relevance"], e["name"]))


def get(name: str) -> dict[str, Any]:
    for e in entries():
        if e["name"] == name or name in e["aliases"]:
            return e
    raise KeyError(f"{name!r} is not in the platform registry")


def snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """The endpoint entry stored on a model at registration time (no host-specific URLs)."""
    keep = ("name", "alias", "aliases", "contract", "api", "engine", "kind", "image", "labels", "signature",
            "description", "tasks", "gpu_fraction", "host_port", "internal_port", "base_path", "health_path")
    return {k: entry.get(k) for k in keep if k in entry}


def public(entry: dict[str, Any]) -> dict[str, Any]:
    return {**snapshot(entry), "base_url": entry.get("base_url"), "url": entry.get("url"),
            "usable_as": entry.get("usable_as"), "relevance": entry.get("relevance"),
            "not_usable": entry.get("not_usable")}


def text_transform_services() -> list[str]:
    try:
        return [e["name"] for e in entries() if e.get("api") == "text.transform"]
    except RegistryUnavailable:
        return []
