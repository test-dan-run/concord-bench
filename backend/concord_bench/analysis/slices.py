"""Scores broken down by label (EVALUATE_PLAN.md §7.3, §7.4): slices, 95% intervals and patterns.

A unit is what a run is scored on: an utterance for ASR runs, a file for VAD / diarisation and pipelines.
Its labels are the file's labels and its reference segment's labels (`datasets.labels`), plus two Bench
derives itself for ASR: `speaker` and `duration_s`. A slice's score always comes from summed counts over
its units (never a mean of unit scores), the same way the run's own score does.

Intervals come from BOOTSTRAP resamples of the slice's units, seeded, so the same data gives the same
numbers. ASR resamples utterances; diarisation and pipelines resample files, so their intervals are wider.
A slice is "worse" when its whole interval is on the bad side of the run's overall score, "better" on the
good side; with fewer than MIN_UNITS units it is "too few" to call; "hard" means the comparison run is also
worse there, so the audio is hard rather than the model weak.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..core.models import AudioFile, Result, RunTask, Segment
from ..datasets import labels as labels_mod

BOOTSTRAP = 1000
SEED = 0
MIN_UNITS = {RunTask.asr: 20, RunTask.seg: 5, RunTask.pipeline: 5}
HEAT_MIN = 10  # heatmap cells with fewer units are shown but not coloured
DEFAULT_BINS = 5

# metric -> (count keys summed per unit, rate from the sums, lower is better)
METRICS: dict[str, tuple[tuple[str, ...], Callable[[dict[str, float]], float | None], bool]] = {
    "wer": (("n", "s", "d", "i"), lambda c: (c["s"] + c["d"] + c["i"]) / c["n"] if c["n"] else None, True),
    "cer": (("cer_n", "cer_e"), lambda c: c["cer_e"] / c["cer_n"] if c["cer_n"] else None, True),
    "mer": (("h", "s", "d", "i"),
            lambda c: (c["s"] + c["d"] + c["i"]) / t if (t := c["h"] + c["s"] + c["d"] + c["i"]) else None, True),
    "wil": (("n", "h", "s", "i"), lambda c: 1 - _wip(c) if _wip(c) is not None else None, True),
    "wip": (("n", "h", "s", "i"), lambda c: _wip(c), False),
    "der": (("missed", "false_alarm", "confusion", "total"),
            lambda c: (c["missed"] + c["false_alarm"] + c["confusion"]) / c["total"] if c["total"] else None, True),
    "vad_error": (("vad_missed", "vad_false_alarm", "vad_total"),
                  lambda c: (c["vad_missed"] + c["vad_false_alarm"]) / c["vad_total"] if c["vad_total"] else None, True),
    "cpwer": (("cp_errors", "cp_n"), lambda c: c["cp_errors"] / c["cp_n"] if c["cp_n"] else None, True),
}
DEFAULT_METRIC = {RunTask.asr: "wer", RunTask.seg: "der", RunTask.pipeline: "cpwer"}


def _wip(c: dict[str, float]) -> float | None:
    p = c["h"] + c["s"] + c["i"]
    if not c["n"]:
        return None
    return (c["h"] / c["n"]) * (c["h"] / p) if p else 0.0


@dataclass
class Unit:
    id: int  # the result row
    file_id: int
    key: str
    counts: dict[str, float]
    labels: dict[str, Any] = field(default_factory=dict)
    wer: float | None = None
    ref: str | None = None
    hyp: str | None = None


def units(results: list[Result], task: str, files: dict[int, AudioFile], segments: dict[int, Segment]) -> list[Unit]:
    """The units of a score with their counts and labels."""
    out = []
    for r in results:
        m = r.metrics or {}
        if task == RunTask.asr:
            counts = {k: float(m.get(k) or 0) for k in ("n", "s", "d", "i", "h", "cer_n", "cer_e")}
        elif task == RunTask.seg:
            counts = {k: float(m.get(k) or 0) for k in ("missed", "false_alarm", "confusion", "total", "vad_missed",
                                                         "vad_false_alarm", "vad_total")}
        else:
            cp = m.get("cp") or {}
            counts = {"cp_errors": float(cp.get("errors") or 0), "cp_n": float(cp.get("n") or 0),
                      **{k: float(m.get(f"wer_{k}") or 0) for k in ("n", "s", "d", "i", "h", "cer_n", "cer_e")}}
        f = files.get(r.audio_file_id)
        lab = dict(labels_mod.of(f.meta if f else {}))
        if task == RunTask.asr:
            seg = segments.get(r.segment_id) if r.segment_id else None
            lab.update(labels_mod.of(seg.meta if seg else {}))
            if r.speaker:
                lab["speaker"] = r.speaker
            lab["duration_s"] = round(r.end - r.start, 3)
        out.append(Unit(r.id, r.audio_file_id, r.utterance_key, counts, lab, r.wer, r.ref_text, r.hyp_text))
    return out


def rate(us: list[Unit], metric: str) -> float | None:
    keys, fn, _ = METRICS[metric]
    return fn({k: sum(u.counts.get(k, 0.0) for u in us) for k in keys})


def interval(us: list[Unit], metric: str, n: int = BOOTSTRAP, seed: int = SEED) -> tuple[float, float] | None:
    """95% bootstrap interval of a slice's score, resampling its units."""
    if not us:
        return None
    keys, fn, _ = METRICS[metric]
    arr = np.array([[u.counts.get(k, 0.0) for k in keys] for u in us])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(us), size=(n, len(us)))
    sums = arr[idx].sum(axis=1)  # (n, keys)
    vals = [fn(dict(zip(keys, row, strict=True))) for row in sums]
    vals = np.array([v for v in vals if v is not None])
    if not len(vals):
        return None
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return float(lo), float(hi)


def pattern(value: float | None, ci: tuple[float, float] | None, overall: float | None, n: int, task: str,
            low_better: bool) -> str:
    if value is None or ci is None or overall is None:
        return "none"
    if n < MIN_UNITS.get(task, 20):
        return "too_few"
    worse = ci[0] > overall if low_better else ci[1] < overall
    better = ci[1] < overall if low_better else ci[0] > overall
    return "worse" if worse else "better" if better else "same"


# ---------------------------------------------------------------- grouping

def label_info(us: list[Unit]) -> list[dict[str, Any]]:
    """Every label on these units with its kind (datasets.labels), how many units carry it, and values."""
    by_key: dict[str, list[Any]] = {}
    for u in us:
        for k, v in u.labels.items():
            by_key.setdefault(k, []).append(v)
    out = []
    for k, vals in sorted(by_key.items()):
        kind, why = labels_mod.kind_of(vals)
        if k == "speaker" and kind == "unused":
            continue
        entry = {"key": k, "kind": kind, "have": len(vals), "of": len(us), "derived": k in ("speaker", "duration_s")}
        if why:
            entry["reason"] = why
        out.append(entry)
    return out


def edges_for(values: list[float], bins: str = "even", k: int = DEFAULT_BINS,
              custom: list[float] | None = None) -> list[float]:
    """Bin edges for a continuous label: even width, quantiles (equal counts), or custom edges."""
    lo, hi = min(values), max(values)
    if bins == "custom" and custom:
        inner = sorted(e for e in custom if lo < e < hi)
        return [lo, *inner, hi]
    if lo == hi:
        return [lo, hi]
    if bins == "quantile":
        qs = sorted(set(float(x) for x in np.quantile(values, np.linspace(0, 1, k + 1))))
        return qs if len(qs) > 1 else [lo, hi]
    return [float(x) for x in np.linspace(lo, hi, k + 1)]


def _bin_of(v: float, edges: list[float]) -> int:
    for i in range(len(edges) - 1):
        if v < edges[i + 1] or i == len(edges) - 2:
            return i
    return len(edges) - 2


def _fmt(x: float) -> str:
    return f"{x:.3g}"


def groups(us: list[Unit], key: str, bins: str = "even", custom: list[float] | None = None) -> list[
        tuple[dict[str, Any], list[Unit]]]:
    """The slices of one label: (slice, its units), in value order. Units without the label are left out."""
    have = [u for u in us if key in u.labels]
    kind, _ = labels_mod.kind_of([u.labels[key] for u in have]) if have else ("unused", None)
    if kind == "unused":
        return []
    if kind == "continuous":
        vals = [labels_mod.number(u.labels[key]) for u in have]
        edges = edges_for([v for v in vals if v is not None], bins, custom=custom)
        out: dict[int, list[Unit]] = {}
        for u, v in zip(have, vals, strict=True):
            if v is not None:
                out.setdefault(_bin_of(v, edges), []).append(u)
        return [({"key": key, "kind": kind, "min": edges[i], "max": edges[i + 1],
                  "value": f"{_fmt(edges[i])} – {_fmt(edges[i + 1])}", "bin": i}, out[i]) for i in sorted(out)]
    by: dict[Any, list[Unit]] = {}
    for u in have:
        by.setdefault(labels_mod.value_key("discrete", u.labels[key]), []).append(u)
    return [({"key": key, "kind": "discrete", "value": v}, by[v]) for v in sorted(by, key=lambda x: str(x))]


def matches(u: Unit, filters: list[dict[str, Any]]) -> bool:
    """A unit in a saved slice: every filter holds ({key, value}, or {key, min, max} for a range)."""
    for f in filters:
        if f["key"] not in u.labels:
            return False
        v = u.labels[f["key"]]
        if "value" in f:
            if labels_mod.value_key("discrete", v) != f["value"] and str(v) != str(f["value"]):
                return False
        else:
            n = labels_mod.number(v)
            if n is None or n < f.get("min", -np.inf) or n > f.get("max", np.inf):
                return False
    return True


# ---------------------------------------------------------------- reports

def slice_row(meta: dict[str, Any], us: list[Unit], metric: str, task: str, overall: float | None,
              other: list[Unit] | None, other_overall: float | None) -> dict[str, Any]:
    _, _, low = METRICS[metric]
    v = rate(us, metric)
    ci = interval(us, metric)
    pat = pattern(v, ci, overall, len(us), task, low)
    row = {**meta, "n": len(us), "score": v, "ci": ci, "delta": v - overall if v is not None and overall is not None
           else None, "pattern": pat}
    if other is not None:
        ov = rate(other, metric) if other else None
        oci = interval(other, metric) if other else None
        row["compare"] = {"n": len(other), "score": ov, "ci": oci}
        if pat == "worse" and pattern(ov, oci, other_overall, len(other), task, low) == "worse":
            row["pattern"] = "hard"
    return row


def label_rows(us: list[Unit], key: str, metric: str, task: str, other: list[Unit] | None = None,
               bins: str = "even", custom: list[float] | None = None) -> list[dict[str, Any]]:
    """One row per slice of a label, the comparison run's units grouped the same way."""
    overall = rate(us, metric)
    other_overall = rate(other, metric) if other else None
    other_by: dict[Any, list[Unit]] = {}
    if other is not None:
        # same edges for both runs: bin the comparison by the first run's slices
        for meta, _ in groups(us, key, bins, custom):
            if meta["kind"] == "continuous":
                other_by[meta["value"]] = [u for u in other if key in u.labels and
                                           meta["min"] <= (labels_mod.number(u.labels[key]) or -np.inf) <= meta["max"]]
            else:
                other_by[meta["value"]] = [u for u in other if key in u.labels and
                                           labels_mod.value_key("discrete", u.labels[key]) == meta["value"]]
    return [slice_row(meta, g, metric, task, overall, other_by.get(meta["value"]) if other is not None else None,
                      other_overall) for meta, g in groups(us, key, bins, custom)]


def finder(us: list[Unit], metric: str, task: str, other: list[Unit] | None = None, limit: int = 40) -> list[dict]:
    """Slices of every usable label, worst first: where the model fails."""
    rows = []
    for info in label_info(us):
        if info["kind"] == "unused":
            continue
        rows += label_rows(us, info["key"], metric, task, other)
    order = {"hard": 0, "worse": 1, "same": 2, "better": 3, "too_few": 4, "none": 5}
    _, _, low = METRICS[metric]
    rows.sort(key=lambda r: (order[r["pattern"]], -(r["delta"] or 0) if low else (r["delta"] or 0)))
    return rows[:limit]


def heatmap(us: list[Unit], row_key: str, col_key: str, metric: str) -> dict[str, Any]:
    rows = groups(us, row_key)
    cols = groups(us, col_key, bins="quantile" if labels_mod.kind_of(
        [u.labels[col_key] for u in us if col_key in u.labels])[0] == "continuous" else "even")
    col_ids = {}
    for meta, g in cols:
        for u in g:
            col_ids[u.id] = meta["value"]
    cells = []
    for rmeta, rg in rows:
        line = []
        for cmeta, _ in cols:
            sub = [u for u in rg if col_ids.get(u.id) == cmeta["value"]]
            line.append({"score": rate(sub, metric) if sub else None, "n": len(sub), "colour": len(sub) >= HEAT_MIN})
        cells.append({"label": rmeta["value"], "cells": line})
    return {"rows": cells, "cols": [m["value"] for m, _ in cols], "overall": rate(us, metric),
            "col_filters": [{"key": col_key, "min": m["min"], "max": m["max"]} if m["kind"] == "continuous"
                            else {"key": col_key, "value": m["value"]} for m, _ in cols],
            "row_filters": [{"key": row_key, "min": m["min"], "max": m["max"]} if m["kind"] == "continuous"
                            else {"key": row_key, "value": m["value"]} for m, _ in rows]}


def worst(us: list[Unit], k: int = 6) -> list[dict[str, Any]]:
    ranked = sorted((u for u in us if u.wer is not None), key=lambda u: -(u.wer or 0))[:k]
    return [{"id": u.id, "key": u.key, "file_id": u.file_id, "score": u.wer, "ref": u.ref, "hyp": u.hyp,
             "labels": {k_: v for k_, v in u.labels.items() if k_ not in ("duration_s",)}} for u in ranked]
