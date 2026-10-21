"""Scores for a run: one `score` row per (run, normaliser, collar) and its per-utterance / per-file
`result` rows. Reads cached outputs only, so rescoring takes seconds and no inference.

- ASR runs: per utterance WER / CER / S / D / I with the word alignment (timed when the model gave
  word times). Collar is not used (stored as 0).
- Seg runs: per file DER / JER / speaker mapping (speaker models) and VAD missed / false alarm (all),
  error regions for the lanes. Normaliser is not used (stored as "-").
- Pipelines: per file WER, cpWER (C), DER of the seg stage, error attribution (A / B / C), RTF.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..core.models import Result, Run, RunTask, Score
from ..metrics import attribution, cpwer, der, normalisers, vad, wer
from ..metrics.der import Turn
from . import cache
from .executor import file_refs
from .references import FileRefs

NO_NORMALISER = "-"


def score_key(run: Run, normaliser: str | None, collar: float | None) -> tuple[str, float]:
    n = normalisers.get(normaliser or normalisers.DEFAULT).key if run.task != RunTask.seg else NO_NORMALISER
    c = float(der.DEFAULT_COLLAR if collar is None else collar) if run.task != RunTask.asr else 0.0
    return n, c


def find_score(session: Session, run: Run, normaliser: str | None, collar: float | None) -> Score | None:
    n, c = score_key(run, normaliser, collar)
    return session.scalar(select(Score).where(Score.run_id == run.id, Score.normaliser == n, Score.collar == c))


def score_run(session: Session, run: Run, normaliser: str | None = None, collar: float | None = None) -> Score:
    if run.status != "done" and not run.outputs:
        raise ValueError(f"run {run.id} has no outputs yet ({run.status})")
    n_key, c = score_key(run, normaliser, collar)
    old = find_score(session, run, normaliser, collar)
    if old is not None:
        session.execute(delete(Result).where(Result.score_id == old.id))
        session.delete(old)
        session.flush()
    score = Score(run_id=run.id, normaliser=n_key, collar=c)
    session.add(score)
    session.flush()
    refs = {str(r.file.id): r for r in file_refs(session, run.dataset_id, include_long=True)}
    norm = normalisers.get(n_key) if n_key != NO_NORMALISER else None
    if run.task == RunTask.asr:
        rows, agg = _score_asr(session, run, refs, norm)
    elif run.task == RunTask.seg:
        rows, agg = _score_seg(session, run, refs, c)
    else:
        rows, agg = _score_pipeline(session, run, refs, norm, c)
    for r in rows:
        r.score_id = score.id
    session.add_all(rows)
    agg["rtf"] = run.rtf_by_stage
    score.aggregates = agg
    session.flush()
    return score


# ---------------------------------------------------------------- helpers

def _timed_tokens(output: dict[str, Any], norm: normalisers.Normaliser, offset: float) -> list[tuple[float, float]] | None:
    """Per normalised hypothesis token, (start, end) in file time, when the model gave word times and
    per-word normalisation yields the same tokens as normalising the whole text."""
    words = output.get("words")
    if not words:
        return None
    toks: list[tuple[float, float]] = []
    per_word = norm.many([w["w"] for w in words])
    for w, nw in zip(words, per_word, strict=True):
        for _ in nw.split():
            toks.append((round(w["start"] + offset, 3), round(w["end"] + offset, 3)))
    return toks


def _attach_times(ops: list[dict[str, Any]], times: list[tuple[float, float]] | None, n_hyp: int,
                  span: tuple[float, float]) -> bool:
    """Give every alignment op a [t0, t1]; deletions sit between their hypothesis neighbours."""
    if times is None or len(times) != n_hyp:
        return False
    prev_end = span[0]
    for k, op in enumerate(ops):
        if op["hi"] is not None:
            op["t0"], op["t1"] = times[op["hi"]]
            prev_end = op["t1"]
        else:
            nxt = next((times[o["hi"]][0] for o in ops[k + 1:] if o["hi"] is not None), span[1])
            op["t0"], op["t1"] = round(prev_end, 3), round(max(prev_end, nxt), 3)
    return True


def _segment_times(output: dict[str, Any], offset: float) -> list[dict[str, Any]] | None:
    segs = output.get("segments")
    if not segs:
        return None
    return [{"start": round(s["start"] + offset, 3), "end": round(s["end"] + offset, 3), "text": s["text"]}
            for s in segs]


# ---------------------------------------------------------------- ASR

def _score_asr(session: Session, run: Run, refs: dict[str, FileRefs], norm) -> tuple[list[Result], dict[str, Any]]:
    out_rows: list[Result] = []
    plan = []  # (file refs, unit, output, offset)
    ids = [u["cache_id"] for f in run.outputs.values() for u in f.get("units", []) if u.get("cache_id")]
    outs = cache.outputs_for(session, ids)
    for fid, entry in run.outputs.items():
        r = refs.get(fid)
        if r is None or not r.has_text:
            continue
        units = {u.key: u for u in r.asr_units()}
        for u in entry.get("units", []):
            unit = units.get(u["key"])
            if unit is None:
                continue
            row = outs.get(u["cache_id"]) if u.get("cache_id") else None
            output = row.output if row else {"text": "", "words": None, "segments": None}
            offset = 0.0 if row is None or row.span_end < 0 else row.span_start
            plan.append((r, unit, output, offset))
    refs_n = norm.many([p[1].text for p in plan])
    hyps_n = norm.many([p[2].get("text", "") for p in plan])
    counts = []
    for (r, unit, output, offset), rn, hn in zip(plan, refs_n, hyps_n, strict=True):
        res = wer.score(rn, hn)
        c = res.counts()
        ops = res.ops_json()
        timed = _attach_times(ops, _timed_tokens(output, norm, offset), len(hn.split()), (unit.start, unit.end))
        c["duration"] = round(unit.end - unit.start, 3)
        counts.append(c)
        out_rows.append(Result(
            audio_file_id=r.file.id, utterance_key=unit.key, segment_id=unit.segment_id, start=unit.start,
            end=unit.end, speaker=unit.speaker, ref_text=unit.text, hyp_text=output.get("text", ""),
            wer=c["wer"], errors=c["errors"], metrics=c,
            alignment={"ops": ops, "timed": timed, "ref_norm": rn, "hyp_norm": hn,
                       "segments": _segment_times(output, offset)}))
    agg = wer.aggregate(counts)
    return out_rows, agg


# ---------------------------------------------------------------- VAD / diarisation

def turn_stats(turns: list[Turn]) -> dict[str, Any]:
    durs = [t.dur for t in turns]
    return {"turns": len(turns), "mean_turn_s": float(np.mean(durs)) if durs else None}


def seg_file_metrics(r: FileRefs, segs: list[list[float]], speakers: list[str] | None, collar: float) -> tuple[
        dict[str, Any], dict[str, Any]]:
    dur = r.duration
    ref = r.speaker_turns
    if not ref:  # text-only references: speech regions from timed text segments
        ref = [Turn(s.start, s.end, s.speaker or "speech") for s in r.text_segments if not r._is_whole(s)]
    hyp = [Turn(s, e, k) for (s, e), k in zip(segs, speakers or ["speech"] * len(segs), strict=True)]
    m: dict[str, Any] = {"duration": dur, "ref_turns": len(ref), "hyp_turns": len(hyp)}
    m.update(vad.detection(ref, hyp, dur, collar))
    al: dict[str, Any] = {"ref": [[t.start, t.end, t.speaker] for t in ref],
                          "hyp": [[t.start, t.end, t.speaker] for t in hyp]}
    if speakers is not None and r.has_speakers:
        d = der.der(ref, hyp, dur, collar)
        m.update({k: v for k, v in d.items() if k != "mapping"})
        al["mapping"] = d["mapping"]
        al["errors"] = der.error_regions(ref, hyp, dur, d["mapping"])
        al["overlaps"] = der.overlaps(ref, dur)
        al["boundary_offsets"] = der.boundary_offsets(ref, hyp)
        # Turns found, by reference turn length: a turn counts as found when the mapped speaker covers half of it.
        inv: dict[str, list[Turn]] = {}
        for t in hyp:
            inv.setdefault(d["mapping"].get(t.speaker, f"hyp:{t.speaker}"), []).append(t)
        found = []
        for t in ref:
            cov = sum(max(0.0, min(t.end, h.end) - max(t.start, h.start)) for h in inv.get(t.speaker, []))
            found.append([round(t.dur, 3), cov >= 0.5 * t.dur])
        al["turns_found"] = found
    else:
        m["der"] = None
        mapping = {t.speaker: "speech" for t in hyp}
        al["errors"] = der.error_regions([Turn(t.start, t.end, "speech") for t in ref],
                                         [Turn(t.start, t.end, "speech") for t in hyp], dur, mapping)
        al["overlaps"] = der.overlaps(ref, dur) if r.has_speakers else []
        al["boundary_offsets"] = der.boundary_offsets(ref, hyp)
    return m, al


def _score_seg(session: Session, run: Run, refs: dict[str, FileRefs], collar: float) -> tuple[
        list[Result], dict[str, Any]]:
    outs = cache.outputs_for(session, [e["cache_id"] for e in run.outputs.values() if e.get("cache_id")])
    rows, metrics = [], []
    for fid, entry in run.outputs.items():
        r = refs.get(fid)
        row = outs.get(entry.get("cache_id"))
        if r is None or row is None or not (r.has_speakers or r.segment_level):
            continue
        m, al = seg_file_metrics(r, row.output["segments"], row.output.get("speakers"), collar)
        metrics.append(m)
        sort_val = m.get("der") if m.get("der") is not None else m.get("vad_error")
        rows.append(Result(audio_file_id=r.file.id, utterance_key=r.file.stem, start=0.0, end=r.duration,
                           wer=sort_val, metrics=m, alignment=al))
    agg = {**vad.aggregate(metrics)}
    if any(m.get("der") is not None for m in metrics):
        agg.update(der.aggregate([m for m in metrics if m.get("der") is not None]))
    return rows, agg


# ---------------------------------------------------------------- pipelines

def _cut_texts(cuts: list[dict[str, Any]], outs: dict[int, Any]) -> list[tuple[dict[str, Any], str]]:
    return [(c, (outs[c["cache_id"]].output.get("text", "") if c.get("cache_id") in outs else "")) for c in cuts]


def _score_pipeline(session: Session, run: Run, refs: dict[str, FileRefs], norm, collar: float) -> tuple[
        list[Result], dict[str, Any]]:
    oracle = run.pipeline.seg_model_id is None
    has_spk = oracle or run.pipeline.seg_model.has_speakers
    ids = []
    for e in run.outputs.values():
        ids += [c["cache_id"] for c in e.get("cuts", []) + e.get("oracle_cuts", []) if c.get("cache_id")]
        if e.get("seg_cache_id"):
            ids.append(e["seg_cache_id"])
    outs = cache.outputs_for(session, ids)
    rows, wer_rows, cp_rows, der_rows, att_rows = [], [], [], [], []
    wrong_pairs: Counter[tuple[str, str]] = Counter()
    for fid, entry in run.outputs.items():
        r = refs.get(fid)
        if r is None or "cuts" not in entry or not r.has_text:
            continue
        cut_texts = _cut_texts(entry["cuts"], outs)
        ref_full = r.full_text() or ""
        hyp_full = " ".join(t for _, t in cut_texts if t)
        # normalise everything for this file in one batch
        spk_ref = r.spk_text
        o_texts = _cut_texts(entry.get("oracle_cuts", []), outs)
        batch = [ref_full, hyp_full] + [s.text or "" for s in spk_ref] + [t for _, t in cut_texts] + \
                [t for _, t in o_texts]
        nb = norm.many(batch)
        ref_n, hyp_n = nb[0], nb[1]
        k = 2
        spk_ref_n = nb[k:k + len(spk_ref)]
        k += len(spk_ref)
        cut_n = nb[k:k + len(cut_texts)]
        k += len(cut_texts)
        o_n = nb[k:]
        w = wer.score(ref_n, hyp_n)
        m: dict[str, Any] = {"duration": r.duration, **{f"wer_{kk}": v for kk, v in w.counts().items()},
                             "wer": w.wer, "cpwer": None}
        wer_rows.append(w.counts())
        al: dict[str, Any] = {"ref_norm": ref_n, "hyp_norm": hyp_n,
                              "hyp_segments": [{"start": c["start"], "end": c["end"], "speaker": c.get("speaker"),
                                                "text": t} for c, t in cut_texts]}
        ref_turns = [(s.start, s.end, s.speaker) for s in spk_ref]
        # seg-stage DER
        if not oracle and entry.get("seg_cache_id") in outs and r.has_speakers:
            so = outs[entry["seg_cache_id"]].output
            sm, sal = seg_file_metrics(r, so["segments"], so.get("speakers"), collar)
            m.update({kk: sm.get(kk) for kk in ("der", "missed", "false_alarm", "confusion", "total", "jer",
                                                 "vad_error", "vad_missed", "vad_false_alarm", "vad_total")})
            al["seg"] = {kk: sal.get(kk) for kk in ("ref", "hyp", "mapping", "errors", "overlaps")}
            if sm.get("der") is not None:
                der_rows.append(sm)
        elif oracle:
            m["der"] = 0.0
            al["seg"] = {"ref": [[s.start, s.end, s.speaker] for s in r.segments if s.speaker],
                         "hyp": [[s.start, s.end, s.speaker] for s in r.segments if s.speaker], "errors": [],
                         "overlaps": der.overlaps(r.speaker_turns, r.duration)}
        # cpWER (C), and attribution (A, B) when the seg model has speakers
        if spk_ref and has_spk:
            ref_segs = [cpwer.SpkSegment(s.start, s.end, s.speaker, t) for s, t in zip(spk_ref, spk_ref_n, strict=True)]
            hyp_c = [cpwer.SpkSegment(c["start"], c["end"], c["speaker"] or "speech", t)
                     for (c, _), t in zip(cut_texts, cut_n, strict=True)]
            res_c = cpwer.cpwer(ref_segs, hyp_c)
            pw = cpwer.per_word(res_c)
            m["cpwer"] = res_c["cpwer"]
            m["cp"] = cpwer.public(res_c)
            cp_rows.append(m["cp"])
            al["cpwer"] = {"pairs": pw["pairs"], "wrong_speaker_words": pw["wrong_speaker_words"],
                           "assignment": res_c["assignment"]}
            for ww in pw["wrong_speaker_words"]:
                wrong_pairs[(ww["word"], f"{ww['ref_speaker']}→{ww['given_to'] or ww['hyp_speaker']}")] += 1
            if oracle:
                a_err = b_err = res_c["errors"]
            else:
                hyp_a = [cpwer.SpkSegment(c["start"], c["end"], c["speaker"], t)
                         for (c, _), t in zip(o_texts, o_n, strict=True)]
                a_err = cpwer.cpwer(ref_segs, hyp_a)["errors"] if hyp_a else res_c["n"]
                hyp_b = [cpwer.SpkSegment(c["start"], c["end"],
                                          attribution.best_ref_speaker(c["src"][0], c["src"][1], ref_turns)
                                          or "unassigned", t) for (c, _), t in zip(cut_texts, cut_n, strict=True)]
                b_err = cpwer.cpwer(ref_segs, hyp_b)["errors"]
            att = attribution.split(a_err, b_err, res_c["errors"], res_c["n"])
            m["attribution"] = att
            att_rows.append(att)
        m["rtf"] = run.rtf_by_stage.get("total")
        sort_val = m["cpwer"] if m["cpwer"] is not None else m["wer"]
        rows.append(Result(audio_file_id=r.file.id, utterance_key=r.file.stem, start=0.0, end=r.duration,
                           ref_text=ref_full, hyp_text=hyp_full, wer=sort_val, errors=w.errors, metrics=m,
                           alignment=al))
    agg: dict[str, Any] = {"wer": wer.aggregate(wer_rows), "has_speakers": has_spk, "oracle": oracle}
    agg["cpwer"] = cpwer.aggregate(cp_rows) if cp_rows else None
    agg["der"] = der.aggregate(der_rows) if der_rows else ({"der": 0.0} if oracle else None)
    agg["attribution"] = attribution.aggregate(att_rows) if att_rows else None
    agg["wrong_speaker_pairs"] = [{"word": w_, "move": mv, "n": n} for (w_, mv), n in wrong_pairs.most_common(20)]
    return rows, agg
