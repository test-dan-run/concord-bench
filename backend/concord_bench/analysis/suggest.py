"""Tag-overlap suggestions for the benchmark-plan panel (upload page, dataset page, Run evaluation)."""

from __future__ import annotations

from typing import Any


def _norm(tags: list[str] | None) -> set[str]:
    return {t.strip().lower().lstrip("#") for t in tags or [] if t.strip()}


def overlap(a: list[str] | None, b: list[str] | None) -> list[str]:
    return sorted(_norm(a) & _norm(b))


def rank_models(dataset_tags: list[str], models: list[dict[str, Any]], language: str | None = None) -> list[dict[str, Any]]:
    """Catalog models ({catalog_id, tags, stages}) with their shared tags, best first; `preselect` when they
    share at least one tag and the catalog has verified them."""
    lang = (language or "").lower()
    out = []
    for m in models:
        shared = overlap(dataset_tags, m.get("tags"))
        lang_hit = bool(lang) and any(lang.split("-")[0] == t.split("-")[0] for t in _norm(m.get("tags")))
        out.append({"catalog_id": m["catalog_id"], "shared_tags": shared, "score": len(shared) + 0.5 * lang_hit,
                    "preselect": bool(shared) and bool(m.get("usable", True)), "stages": list(m.get("stages") or [])})
    return sorted(out, key=lambda r: (-r["score"], r["catalog_id"]))
