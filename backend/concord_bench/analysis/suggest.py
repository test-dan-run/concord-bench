"""Tag-overlap suggestions, shared by the upload benchmark-plan panel and the add-model datasets panel."""

from __future__ import annotations

from typing import Any

from ..core.models import Dataset, Model


def _norm(tags: list[str] | None) -> set[str]:
    return {t.strip().lower().lstrip("#") for t in tags or [] if t.strip()}


def overlap(a: list[str] | None, b: list[str] | None) -> list[str]:
    return sorted(_norm(a) & _norm(b))


def stage_of(model: Model) -> list[str]:
    out = []
    if model.is_seg:
        out.append("seg")
    if model.is_asr:
        out.append("asr")
    return out


def labels_needed(model: Model) -> set[str]:
    need = set()
    if model.is_asr:
        need.add("asr")
    if model.is_seg:
        need.add("diar")
    return need


def rank_models(dataset_tags: list[str], models: list[Model], language: str | None = None) -> list[dict[str, Any]]:
    """Models with their shared tags, best first; `preselect` when they share at least one tag."""
    lang = (language or "").lower()
    out = []
    for m in models:
        shared = overlap(dataset_tags, m.tags)
        lang_hit = bool(lang) and any(lang.split("-")[0] == t.split("-")[0] for t in _norm(m.tags))
        out.append({"model_id": m.id, "shared_tags": shared, "score": len(shared) + 0.5 * lang_hit,
                    "preselect": bool(shared), "stages": stage_of(m)})
    return sorted(out, key=lambda r: (-r["score"], r["model_id"]))


def rank_datasets(model_tags: list[str], capabilities: list[str], datasets: list[Dataset]) -> list[dict[str, Any]]:
    """Datasets with labels for the detected tasks, ranked by shared tags."""
    need = set()
    if "asr" in capabilities:
        need.add("asr")
    if {"vad", "diar"} & set(capabilities):
        need.add("diar")
    out = []
    for d in datasets:
        if d.status != "ready":
            continue
        has = need & set(d.label_types or [])
        if not has:
            continue
        shared = overlap(model_tags, d.tags)
        out.append({"dataset_id": d.id, "name": d.name, "shared_tags": shared, "score": len(shared),
                    "label_types": sorted(has), "preselect": bool(shared)})
    return sorted(out, key=lambda r: (-r["score"], r["name"]))
