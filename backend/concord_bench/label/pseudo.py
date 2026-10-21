"""Ensemble pseudo-labelling as a `label` run (PLAN §6.2).

A label run is a seg -> ASR pipeline without a reference: the seg model's segments are merged and padded
exactly like a pipeline's (so outputs saved by earlier pipeline runs are cache hits), each cut is
transcribed by every ASR member, and the members are fused per cut. Members already in the cache for
every cut cost nothing; the run then needs no service at all ("Re-fuse 4 saved outputs").

Writing segments follows one rule: reviewed work (confirmed, or touched by a person) is never replaced.
Earlier machine segments nobody touched are replaced; a new segment that mostly overlaps reviewed
work is dropped.

`run.params`: tasks (asr, diar), seg_model_id, asr_model_ids, strategy, single_model_id, high, low.
`run.outputs`: {"files": {file_id: {seg_cache_id, cuts: [{start, end, speaker, src, members}]}},
"summary": {...}}.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import adapters
from ..core import paths
from ..core.models import AudioFile, Dataset, Model, ModelOutput, Run, Segment, utcnow
from ..datasets.audio import cut, load_derived
from ..platform.services import image_info
from ..runs import cache
from ..runs.executor import prepare_cuts, usable_files
from . import fusion

Log = Callable[[str], None]
Progress = Callable[[float, str], None]

OVERLAP_DROP = 0.5  # a new segment overlapping reviewed work by more than this share of itself is dropped
MIN_ASR_SPAN = 0.05


class LabelError(Exception):
    pass


def _models(session: Session, params: dict[str, Any]) -> tuple[Model, list[Model]]:
    seg = session.get(Model, params.get("seg_model_id") or -1)
    if seg is None or not seg.is_seg:
        raise LabelError("pseudo-labelling needs a VAD / diarisation model for the segments")
    asr = [session.get(Model, i) for i in params.get("asr_model_ids") or []]
    if any(m is None or not m.is_asr for m in asr):
        raise LabelError("every ensemble member must be a registered ASR model")
    if "asr" in params.get("tasks", []) and not asr:
        raise LabelError("transcripts need at least one ASR model")
    if "diar" in params.get("tasks", []) and not seg.has_speakers:
        raise LabelError(f"{seg.display_name} returns no speakers; pick a diariser for the speakers task")
    return seg, asr


def asr_span(c_start: float, c_end: float, duration: float) -> tuple[float, float] | None:
    """The cache span the executor uses for a cut (whole file -> (0, WHOLE)); None when too short."""
    if c_end - c_start < MIN_ASR_SPAN:
        return None
    if c_start <= 0.0 and c_end >= duration - 1e-3:
        return 0.0, cache.WHOLE
    return round(c_start, 3), round(c_end, 3)


def _cached_spans(session: Session, service: str, phash: str, shas: list[str]) -> dict[str, set[tuple[float, float]]]:
    out: dict[str, set[tuple[float, float]]] = {}
    rows = session.execute(select(ModelOutput.audio_sha256, ModelOutput.span_start, ModelOutput.span_end)
                           .where(ModelOutput.model_name == service, ModelOutput.params_hash == phash,
                                  ModelOutput.audio_sha256.in_(shas)))
    for sha, s, e in rows:
        out.setdefault(sha, set()).add((s, e))
    return out


def preview(session: Session, ds: Dataset, seg_model: Model, asr_models: list[Model]) -> dict[str, Any]:
    """What a label run would cost: per member, how many cuts are already in the cache.

    Cuts are only known where the seg model's output is cached; elsewhere every member needs inference."""
    files = usable_files(session, ds.id)
    seg = adapters.for_model(seg_model)
    seg_rows = {r.audio_sha256: r for r in session.scalars(select(ModelOutput).where(
        ModelOutput.model_name == seg.service, ModelOutput.params_hash == seg.params_hash,
        ModelOutput.span_end == cache.WHOLE, ModelOutput.audio_sha256.in_([f.sha256 for f in files])))}
    plans: list[tuple[AudioFile, list[tuple[float, float]] | None]] = []
    for f in files:
        row = seg_rows.get(f.sha256)
        if row is None:
            plans.append((f, None))
            continue
        cuts = prepare_cuts(row.output["segments"], row.output.get("speakers"), f.duration or 0.0)
        plans.append((f, [sp for c in cuts if (sp := asr_span(c.start, c.end, f.duration or 0.0))]))
    members = []
    for m in asr_models:
        a = adapters.for_model(m, ds.language)
        have = _cached_spans(session, a.service, a.params_hash, [f.sha256 for f in files])
        cached = total = unknown = 0
        for f, spans in plans:
            if spans is None:
                unknown += 1
                continue
            total += len(spans)
            cached += sum(1 for sp in spans if sp in have.get(f.sha256, set()))
        saved = unknown == 0 and cached == total
        members.append({"model_id": m.id, "name": m.display_name, "service": m.registry_name,
                        "timing_level": m.timing_level, "cached_cuts": cached, "known_cuts": total,
                        "files_without_segments": unknown,
                        "state": "saved" if saved else ("partial" if cached else "new")})
    seg_cached = sum(1 for _, s in plans if s is not None)
    return {"files": len(files), "seg": {"model_id": seg_model.id, "name": seg_model.display_name,
                                         "service": seg_model.registry_name, "cached_files": seg_cached,
                                         "state": "saved" if seg_cached == len(files) else
                                         ("partial" if seg_cached else "new")},
            "members": members,
            "saved_members": sum(1 for m in members if m["state"] == "saved"),
            "new_members": sum(1 for m in members if m["state"] != "saved")}


def required_services(pv: dict[str, Any]) -> list[str]:
    """Services a label run must start: only stages that are not fully in the cache."""
    names = [m["service"] for m in pv["members"] if m["state"] != "saved"]
    if pv["seg"]["state"] != "saved":
        names.append(pv["seg"]["service"])
    return sorted(set(names))


# ---------------------------------------------------------------- execution

def execute(session: Session, run: Run, log: Log, progress: Progress) -> None:
    ds: Dataset = run.dataset
    p = run.params
    seg_model, asr_models = _models(session, p)
    tasks = set(p.get("tasks") or ["asr", "diar"])
    files = usable_files(session, ds.id)
    if not files:
        raise LabelError("the dataset has no usable audio files")
    seg = adapters.for_model(seg_model)
    members = [adapters.for_model(m, ds.language) for m in asr_models] if "asr" in tasks else []
    run.status, run.started_at = "running", utcnow()
    run.image_tags = {n: image_info(n) for n in [seg.service] + [a.service for a in members]}
    run.params = {**p, "seg_adapter": seg.params, "asr_adapters": {a.service: a.params for a in members}}
    session.commit()
    out_files: dict[str, Any] = {}
    calls = hits = 0
    seg_s = asr_s = audio_s = 0.0
    for k, f in enumerate(files):
        progress(k / len(files), f"{seg_model.display_name} → {len(members)} ASR: {f.rel_path}")
        audio = load_derived(paths.derived(f.sha256))
        row, _ = cache.get_or_run(session, seg, audio, f.sha256, image=seg.options.get("image"))
        seg_s += row.latency_s
        dur = f.duration or 0.0
        cuts = prepare_cuts(row.output["segments"], row.output.get("speakers"), dur)
        entry: dict[str, Any] = {"seg_cache_id": row.id, "cuts": []}
        for c in cuts:
            cd: dict[str, Any] = {"start": c.start, "end": c.end, "speaker": c.speaker, "src": c.src, "members": {}}
            span = asr_span(c.start, c.end, dur)
            for a in members:
                if span is None:
                    continue
                s, e = span
                samples = audio if e == cache.WHOLE else cut(audio, s, e)
                mo, hit = cache.get_or_run(session, a, samples, f.sha256, s, e, a.options.get("image"))
                cd["members"][a.service] = mo.id
                asr_s += mo.latency_s
                calls += 1
                hits += hit
            entry["cuts"].append(cd)
        out_files[str(f.id)] = entry
        audio_s += dur
        session.commit()  # keep cache rows, release the write lock
    run.outputs = {"files": out_files}
    run.rtf_by_stage = {"seg": seg_s / audio_s if audio_s else None, "asr": asr_s / audio_s if audio_s else None,
                        "total": (seg_s + asr_s) / audio_s if audio_s else None, "audio_s": audio_s,
                        "processing_s": seg_s + asr_s}
    log(f"label: {len(files)} files, {calls} ASR calls ({hits} from cache)")
    progress(0.9, "fusing")
    summary = write_segments(session, run, files)
    run.outputs = {"files": out_files, "summary": summary}
    run.finished_at = utcnow()
    session.flush()
    t = summary["triage"]
    log(f"fused {summary['segments']} segments: {t.get('auto', 0)} auto-accepted, {t.get('pseudo', 0)} to review, "
        f"{t.get('rejected', 0)} rejected, {summary['kept_reviewed']} left to reviewed work")


def is_machine(s: Segment) -> bool:
    """Written by pseudo-labelling, here or in another install (imported packages drop the run)."""
    return s.source_run_id is not None or (s.meta or {}).get("source") == "pseudo-label"


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def write_segments(session: Session, run: Run, files: list[AudioFile]) -> dict[str, Any]:
    """Fuse the run's member outputs per cut and write the segments (see the module docstring)."""
    p = run.params
    tasks = set(p.get("tasks") or ["asr", "diar"])
    strategy = p.get("strategy") or "best_aligned"
    single = None
    if strategy == "single":
        m = session.get(Model, p.get("single_model_id") or -1)
        single = m.registry_name if m else None
    high, low = float(p.get("high", fusion.DEFAULT_HIGH)), float(p.get("low", fusion.DEFAULT_LOW))
    services = list((p.get("asr_adapters") or {}).keys())
    ids = [i for e in run.outputs["files"].values() for c in e["cuts"] for i in c["members"].values()]
    outs = cache.outputs_for(session, ids)
    matrix = fusion.AgreementMatrix(services)
    chosen: Counter[str] = Counter()
    applied: Counter[str] = Counter()
    triage: Counter[str] = Counter()
    kept_reviewed = replaced = written = 0
    by_id = {f.id: f for f in files}
    for fid, entry in run.outputs["files"].items():
        f = by_id.get(int(fid))
        if f is None:
            continue
        existing = list(session.scalars(select(Segment).where(Segment.audio_file_id == f.id)))
        reviewed = [s for s in existing if s.reviewed]
        for s in existing:
            if not s.reviewed and is_machine(s):
                session.delete(s)
                replaced += 1
        for c in entry["cuts"]:
            s0, s1 = c["src"]
            if s1 - s0 <= 0:
                continue
            if any(_overlap(s0, s1, r.start, r.end) > OVERLAP_DROP * (s1 - s0) for r in reviewed):
                kept_reviewed += 1
                continue
            meta: dict[str, Any] = {"span": [c["start"], c["end"]], "members": c["members"]}
            text = conf = None
            if "asr" in tasks:
                hyps = []
                for svc in services:
                    row = outs.get(c["members"].get(svc))
                    if row is None:
                        continue
                    o = row.output
                    off = 0.0 if row.span_end < 0 else row.span_start
                    words = [{"w": w["w"], "start": round(w["start"] + off, 3), "end": round(w["end"] + off, 3)}
                             for w in o.get("words") or []] or None
                    hyps.append(fusion.Hyp(svc, o.get("text") or "", words))
                if not hyps:
                    continue
                fz = fusion.fuse(hyps, strategy, single)
                matrix.add(hyps)
                chosen[fz.chosen] += 1
                applied[fz.strategy] += 1
                text, conf = fz.text, fz.confidence
                meta.update({"chosen": fz.chosen, "strategy": fz.strategy, "agreement": fz.agreement,
                             "words": fz.words})
            status = fusion.triage(conf, high, low)
            triage[status] += 1
            session.add(Segment(audio_file_id=f.id, start=round(s0, 3), end=round(s1, 3),
                                speaker=c["speaker"] if "diar" in tasks else None, text=text, status=status,
                                confidence=None if conf is None else round(conf, 4), source_run_id=run.id,
                                meta=meta))
            written += 1
        session.flush()
    total = sum(chosen.values())
    return {"segments": written, "files": len(run.outputs["files"]), "triage": dict(triage),
            "kept_reviewed": kept_reviewed, "replaced": replaced, "agreement": matrix.to_json(),
            "chosen": [{"member": k, "n": v, "share": v / total if total else None} for k, v in chosen.most_common()],
            "strategy_applied": dict(applied), "thresholds": {"high": high, "low": low}}
