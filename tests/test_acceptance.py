"""Phase 1 acceptance (HANDOFF §3.7) end to end on recorded model responses (no GPU, no platform).

1. Upload the labelled fixture dataset; register 2 ASR + 3 VAD / diarisation models with 0 typed fields.
2. Run single-model benchmarks and the full pipeline grid incl. the oracle; every stored number is
   recomputed here with the reference libraries called directly (jiwer, pyannote.metrics, meeteval).
3. Every error-analysis endpoint loads, sorts, expands and serves audio for the fixture set
   (the UI side is covered by the Playwright smoke test).
"""

from __future__ import annotations

import json

import jiwer
import pytest
from meeteval.wer.wer.cp import cp_word_error_rate
from pyannote.core import Annotation, Segment, Timeline
from pyannote.metrics.diarization import DiarizationErrorRate
from sqlalchemy import select

from concord_bench.acceptance import ASR_MODELS, SEG_MODELS, run_flow
from concord_bench.core.models import AudioFile, ModelOutput, Result, Run, Score
from concord_bench.metrics import normalisers
from concord_bench.metrics.cpwer import SpkSegment, streams
from conftest import RECORDED


@pytest.fixture
def flow(replay, client, tmp_path, session):
    out = run_flow(client, tmp_path / "staging", log=lambda m: None)
    return out


def test_registration_needs_no_typed_fields(flow):
    assert flow["typed_fields"] == 0
    assert set(flow["models"]) == set(ASR_MODELS + SEG_MODELS)
    caps = {n: (m["capabilities"], m["timing_level"]) for n, m in flow["models"].items()}
    assert caps["faster-whisper-large-v3"] == (["asr"], "word")
    assert caps["whisper-large-v3"] == (["asr"], "segment")
    assert caps["silero-vad"] == (["vad"], "segment")
    assert caps["pyannote-diarization"] == (["diar"], "segment")
    assert caps["nemotron-3-diarization"] == (["diar"], "segment")


def test_grid_complete(flow):
    ds = flow["dataset"]["id"]
    asr = [c for c in flow["asr"]["cells"] if c["dataset_id"] == ds]
    seg = [c for c in flow["diar"]["cells"] if c["dataset_id"] == ds]
    assert len(asr) == 2 and all(c["value"] is not None for c in asr)
    assert len(seg) == 3 and all(c["value"] is not None for c in seg)
    grid = flow["pipelines"]
    assert len(grid["rows"]) == 4 and len(grid["cols"]) == 2
    assert all(c["score_id"] for c in grid["cells"])  # 4 x 2 pipelines incl. the oracle, all scored
    speech_only = [c for c in grid["cells"] if not c["has_speakers"]]
    assert speech_only and all(c["cpwer"] is None and c["wer"] is not None for c in speech_only)
    oracle = [c for c in grid["cells"] if c["oracle"]]
    assert all(c["cpwer"] is not None and c["attribution"]["segmentation"] == 0 for c in oracle)
    assert all(c["rtf"] is not None for c in grid["cells"])


def test_matches_recorded_summary(flow):
    summary = json.loads((RECORDED / "summary.json").read_text())
    got = sorted((c["label"], round(c["cpwer"] or -1, 6), round(c["wer"], 6)) for c in flow["pipelines"]["runs"])
    want = sorted((p["label"], round(p["cpwer"] or -1, 6), round(p["wer"], 6)) for p in summary["pipelines"])
    assert got == want


def test_asr_scores_match_jiwer(flow, session):
    norm = normalisers.get("basic")
    for run in session.scalars(select(Run).where(Run.task == "asr")):
        sc = session.scalar(select(Score).where(Score.run_id == run.id, Score.normaliser == "basic@1"))
        rows = session.scalars(select(Result).where(Result.score_id == sc.id)).all()
        refs = [norm(r.ref_text) for r in rows]
        hyps = [norm(r.hyp_text) for r in rows]
        assert sc.aggregates["wer"] == pytest.approx(jiwer.wer(refs, hyps))
        assert sc.aggregates["cer"] == pytest.approx(jiwer.cer(refs, hyps))
        for r, ref, hyp in zip(rows, refs, hyps, strict=True):
            if ref:
                assert r.wer == pytest.approx(jiwer.wer(ref, hyp))


def _ann(turns):
    a = Annotation()
    for k, (s, e, spk) in enumerate(turns):
        a[Segment(s, e), k] = spk
    return a


def test_der_matches_pyannote(flow, session):
    for run in session.scalars(select(Run).where(Run.task == "seg")):
        if not run.model.has_speakers:
            continue
        sc = session.scalar(select(Score).where(Score.run_id == run.id, Score.collar == 0.25))
        metric = DiarizationErrorRate(collar=0.5)
        for fid, entry in run.outputs.items():
            out = session.get(ModelOutput, entry["cache_id"]).output
            f = session.get(AudioFile, int(fid))
            ref = [(s.start, s.end, s.speaker) for s in f.segments if s.speaker]
            hyp = [(s, e, k) for (s, e), k in zip(out["segments"], out["speakers"], strict=True)]
            metric(_ann(ref), _ann(hyp), uem=Timeline([Segment(0, f.duration)]))
        assert sc.aggregates["der"] == pytest.approx(abs(metric))


def test_cpwer_matches_meeteval(flow, session):
    norm = normalisers.get("basic")
    for run in session.scalars(select(Run).where(Run.task == "pipeline")):
        sc = session.scalar(select(Score).where(Score.run_id == run.id, Score.normaliser == "basic@1"))
        if not sc.aggregates.get("cpwer"):
            continue
        errors = length = 0
        for r in session.scalars(select(Result).where(Result.score_id == sc.id)):
            f = session.get(AudioFile, r.audio_file_id)
            ref = [SpkSegment(s.start, s.end, s.speaker, norm(s.text)) for s in f.segments if s.speaker and s.text]
            hyp = [SpkSegment(h["start"], h["end"], h["speaker"] or "speech", norm(h["text"]))
                   for h in r.alignment["hyp_segments"]]
            res = cp_word_error_rate(streams(ref), streams(hyp), reference_sort=False, hypothesis_sort=False)
            assert r.metrics["cpwer"] == pytest.approx(res.error_rate)
            errors += res.errors
            length += res.length
        assert sc.aggregates["cpwer"]["cpwer"] == pytest.approx(errors / length)


def test_rescoring_needs_no_inference(flow, client, session):
    n_outputs = session.query(ModelOutput).count()
    run = session.scalar(select(Run).where(Run.task == "asr"))
    r = client.post(f"/api/runs/{run.id}/score", json={"normaliser": "whisper_en"}).json()
    assert r["pending"] is False and r["score"]["normaliser"] == "whisper_en@1"
    seg = session.scalar(select(Run).where(Run.task == "seg"))
    assert client.post(f"/api/runs/{seg.id}/score", json={"collar": 0.0}).json()["score"]["collar"] == 0.0
    assert session.query(ModelOutput).count() == n_outputs


def test_error_views_load_sort_expand_play(flow, client, session):
    asr = session.scalar(select(Run).where(Run.task == "asr", Run.model.has()))
    s = client.get(f"/api/errors/asr/summary?run_id={asr.id}").json()
    assert s["tiles"]["wer"] is not None and s["words"] is not None
    for sort in ("wer", "key", "duration", "s", "d", "i", "n"):
        page = client.get(f"/api/errors/asr/utterances?score_id={s['score_id']}&sort={sort}&order=asc&limit=500").json()
        assert page["total"] == len(page["rows"]) > 0
    top = client.get(f"/api/errors/asr/utterances?score_id={s['score_id']}&sort=wer&order=desc").json()["rows"]
    assert top[0]["wer"] >= top[-1]["wer"]
    detail = client.get(f"/api/errors/asr/utterances/{top[0]['id']}").json()
    assert detail["ops"] and isinstance(detail["others"], list)
    assert client.get(f"/api/audio/{detail['file_id']}", headers={"Range": "bytes=0-1023"}).status_code == 206
    csv = client.get(f"/api/errors/asr/utterances.csv?score_id={s['score_id']}").text
    assert csv.startswith("utterance,speaker") and csv.count("\n") == page["total"] + 1
    # flag a reference
    seg_id = detail["segment_id"]
    assert client.post(f"/api/segments/{seg_id}/flag", json={"flagged": True}).json()["flagged"] is True

    seg = session.scalar(select(Run).where(Run.task == "seg"))
    ds = client.get(f"/api/errors/diar/summary?run_id={seg.id}").json()
    files = client.get(f"/api/errors/diar/files?score_id={ds['score_id']}&sort=der").json()["rows"]
    assert len(files) == 6
    fd = client.get(f"/api/errors/diar/files/{files[0]['id']}").json()
    assert fd["ref"] and fd["hyp"] and "errors" in fd

    for pipe in session.scalars(select(Run).where(Run.task == "pipeline")):
        ps = client.get(f"/api/errors/pipeline/summary?run_id={pipe.id}").json()
        rows = client.get(f"/api/errors/pipeline/files?score_id={ps['score_id']}&sort=cpwer").json()["rows"]
        assert len(rows) == 6
        pf = client.get(f"/api/errors/pipeline/files/{rows[0]['id']}").json()
        assert pf["hyp_segments"] and pf["ref_segments"]
        if ps["has_speakers"] and not ps["oracle"]:
            assert len(ps["counterfactuals"]) == 3
            att = ps["tiles"]["attribution"]
            assert att["asr_errors"] + att["segmentation_errors"] + att["speaker_errors"] == att["c_errors"]
