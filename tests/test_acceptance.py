"""Phase 1 acceptance (HANDOFF §3.7) end to end on recorded model responses (no GPU, no platform).

1. Upload the labelled fixture dataset; pick 2 ASR + 3 VAD / diarisation models from the model catalog (the
   replay stand-in) with 0 typed fields.
2. Run single-model benchmarks and the full pipeline grid incl. the oracle, stage by stage; every stored
   number is recomputed here with the reference libraries called directly (jiwer, pyannote.metrics, meeteval).
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


def test_models_come_from_the_catalog_with_no_typed_fields(flow):
    assert flow["typed_fields"] == 0
    assert set(flow["models"]) == set(ASR_MODELS + SEG_MODELS)
    caps = {n: (m["capabilities"], m["timing_level"]) for n, m in flow["models"].items()}
    assert caps["faster-whisper-large-v3"] == (["asr"], "word")
    assert caps["whisper-large-v3"] == (["asr"], "segment")
    assert caps["silero-vad"] == (["vad"], None)
    assert caps["pyannote-diarization"] == (["diar"], None)
    assert caps["nemotron-3-diarization"] == (["diar"], None)
    assert all(m["build_key"] and m["usable"] for m in flow["models"].values())


def test_grid_runs_stage_by_stage(flow, session):
    """Each segmenter once over the dataset, each ASR model once over every pipeline's cuts, then one
    assembly per pipeline that leases nothing; the plan was previewed through the catalog first."""
    from concord_bench.core.models import Job
    from concord_bench.platform import catalog

    plan = flow["plan"]
    assert plan["runs"] == 8 and plan["error"] is None
    assert [(st["stage"], st["model"]) for st in plan["plan"]["steps"]] == (
        [("seg", m) for m in SEG_MODELS] + [("asr", m) for m in ASR_MODELS])
    by_type: dict[str, list[Job]] = {}
    for j in session.query(Job).order_by(Job.id):
        by_type.setdefault(j.type, []).append(j)
    assert sorted(j.payload["model_id"] for j in by_type["seg_stage"]) == sorted(
        {j.payload["model_id"] for j in by_type["seg_stage"]})
    assert len(by_type["seg_stage"]) == 3 and len(by_type["asr_stage"]) == 2
    assert all(len(j.payload["sources"]) == 4 for j in by_type["asr_stage"])  # 3 segmenters + the oracle
    assembled = [j for j in by_type["run"] if j.payload.get("assemble")]
    assert len(assembled) == 8 and all(j.required_services == [] and j.after for j in assembled)
    leases = [c[1] for c in catalog.get().calls if c[0] == "lease"]
    holders = {f"bench:job-{j.id}" for j in by_type["seg_stage"] + by_type["asr_stage"]}
    stage_leases = [models for holder, models in leases if holder in holders]
    # the single seg runs before stored every whole-file output, so the seg stages lease nothing; each ASR
    # model is leased once for every pipeline's cuts
    assert sorted(m for (m,) in stage_leases) == sorted(ASR_MODELS)
    assert all(j.result["computed"] == 0 and j.result["hits"] == 6 for j in by_type["seg_stage"])
    assert all(j.result["computed"] > 0 for j in by_type["asr_stage"])
    assert all(j.status == "done" and j.result["build_key"] for j in by_type["seg_stage"] + by_type["asr_stage"])


def test_outputs_are_indexed_with_their_build(flow, session):
    from concord_bench.platform import catalog

    rows = session.query(ModelOutput).all()
    assert rows and all(r.build_key and r.params and "image" not in r.params for r in rows)
    keys = [{"build_key": r.build_key, "audio_sha256": r.audio_sha256, "span_start": r.span_start,
             "span_end": r.span_end, "params_hash": r.params_hash} for r in rows]
    hits = catalog.get().index_lookup("label", keys)
    assert [h["output_id"] for h in hits] == [r.id for r in rows] and {h["owner"] for h in hits} == {"bench"}


def test_rerun_needs_no_model(flow, client, session):
    """Everything is stored: a second grid (new runs) leases nothing and calls no model."""
    from concord_bench.core.models import Run
    from concord_bench.platform import catalog
    from concord_bench.runs import jobs

    cat = catalog.get()
    n_outputs = session.query(ModelOutput).count()
    leases = len([c for c in cat.calls if c[0] == "lease"])
    before = session.query(Run).filter(Run.task == "pipeline").count()
    r = client.post("/api/benchmarks/plan", json={"dataset_id": flow["dataset"]["id"], "seg_models": SEG_MODELS,
                                                  "asr_models": ASR_MODELS, "oracle": True, "mode": "grid"})
    assert r.json()["queued"] == 8
    jobs.run_pending()
    session.expire_all()
    assert session.query(ModelOutput).count() == n_outputs
    assert len([c for c in cat.calls if c[0] == "lease"]) == leases
    runs = session.query(Run).filter(Run.task == "pipeline").all()
    assert len(runs) == before + 8 and all(r.status == "done" for r in runs)


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


# ---------------------------------------------------------------- EVALUATE_PLAN.md E1

def test_asr_mer_wil_wip_match_jiwer(flow, session):
    norm = normalisers.get("basic")
    for run in session.scalars(select(Run).where(Run.task == "asr")):
        sc = session.scalar(select(Score).where(Score.run_id == run.id, Score.normaliser == "basic@1"))
        rows = session.scalars(select(Result).where(Result.score_id == sc.id)).all()
        out = jiwer.process_words([norm(r.ref_text) for r in rows], [norm(r.hyp_text) for r in rows])
        assert sc.aggregates["mer"] == pytest.approx(out.mer)
        assert sc.aggregates["wil"] == pytest.approx(out.wil)
        assert sc.aggregates["wip"] == pytest.approx(out.wip)


def test_tcpwer_matches_meeteval(flow, session):
    from meeteval.io.seglst import SegLST
    from meeteval.wer.wer.time_constrained import time_constrained_minimum_permutation_word_error_rate as tcp

    from concord_bench.metrics import cpwer

    norm = normalisers.get("basic")

    def seglst(segs):
        return SegLST([{"session_id": "f", "speaker": s.speaker, "start_time": s.start, "end_time": s.end,
                        "words": s.text} for s in segs])

    checked = 0
    for run in session.scalars(select(Run).where(Run.task == "pipeline")):
        sc = session.scalar(select(Score).where(Score.run_id == run.id, Score.normaliser == "basic@1"))
        if not sc.aggregates.get("cpwer"):
            continue
        for collar in cpwer.TCP_COLLARS:
            errors = length = 0
            for r in session.scalars(select(Result).where(Result.score_id == sc.id)):
                f = session.get(AudioFile, r.audio_file_id)
                ref = [SpkSegment(s.start, s.end, s.speaker, norm(s.text)) for s in f.segments if s.speaker and s.text]
                hyp = [SpkSegment(h["start"], h["end"], h["speaker"] or "speech", norm(h["text"]))
                       for h in r.alignment["hyp_segments"]]
                res = tcp(seglst(ref), seglst(hyp), collar=collar)
                assert r.metrics["tcp"][cpwer.tcp_key(collar)]["errors"] == res.errors
                errors += res.errors
                length += res.length
            assert sc.aggregates["tcpwer"][cpwer.tcp_key(collar)]["tcpwer"] == pytest.approx(errors / length)
            # timing can only add errors to cpWER
            assert errors >= sc.aggregates["cpwer"]["errors"]
        checked += 1
    assert checked


def test_runs_record_hardware_and_wall_time(flow, session):
    for run in session.scalars(select(Run)):
        assert run.wall_s is not None and run.wall_s >= 0
        assert set(run.hardware) == set(run.builds)
        assert all(hw["device"] in ("gpu", "cpu") and hw["image"] for hw in run.hardware.values())
    asr = flow["asr"]["cells"][0]
    assert asr["hardware"] and asr["wall_s"] is not None and asr["metrics"]["mer"] is not None


def test_benchmark_metric_and_overlap_parameters(flow, client):
    mer = client.get("/api/benchmarks/asr", params={"metric": "mer"}).json()
    by_id = {(c["model_id"], c["dataset_id"]): c for c in flow["asr"]["cells"]}
    for c in mer["cells"]:
        if c["value"] is not None:
            assert c["metric"] == "MER" and c["value"] == pytest.approx(by_id[(c["model_id"], c["dataset_id"])]
                                                                         ["metrics"]["mer"])
    assert client.get("/api/benchmarks/asr", params={"metric": "xyz"}).status_code == 400
    skipped = client.get("/api/benchmarks/diar", params={"skip_overlap": True}).json()
    assert skipped["skip_overlap"] is True
    scored = {(c["model_id"], c["dataset_id"]): c for c in flow["diar"]["cells"]}
    for c in skipped["cells"]:
        if c["metric"] == "DER" and c["value"] is not None:
            assert c["score_id"] != scored[(c["model_id"], c["dataset_id"])]["score_id"]
            assert c["breakdown"]["facts"]["Reference speech (s)"] <= \
                scored[(c["model_id"], c["dataset_id"])]["breakdown"]["facts"]["Reference speech (s)"]
    grid = client.get("/api/benchmarks-pipelines", params={"tcp_collar": 2}).json()
    assert grid["tcp_collar"] == 2 and any(c.get("tcpwer") is not None for c in grid["cells"])


# ---------------------------------------------------------------- EVALUATE_PLAN.md E5c, E6

def test_slices_by_label_and_saved_slice_columns(flow, client, session):
    asr = session.scalar(select(Run).where(Run.task == "asr", Run.model.has()))
    s = client.get(f"/api/errors/asr/summary?run_id={asr.id}").json()
    out = client.get("/api/errors/slices", params={"score_id": s["score_id"]}).json()
    assert out["overall"]["value"] == pytest.approx(s["tiles"]["wer"])
    keys = {lb["key"] for lb in out["labels"]}
    assert {"speaker", "duration_s"} <= keys  # derived labels exist even without labels in the references
    rows = client.get("/api/errors/slices", params={"score_id": s["score_id"], "label": "duration_s",
                                                    "bins": "quantile"}).json()["rows"]
    assert rows and sum(r["n"] for r in rows) == out["overall"]["n"]
    assert all(r["ci"] is None or r["ci"][0] <= r["score"] <= r["ci"][1] for r in rows)
    # save a slice on the dataset: it becomes a Benchmarks column for every model on that dataset
    spk = next(lb for lb in out["labels"] if lb["key"] == "speaker")
    assert spk["kind"] != "unused"
    first = client.get("/api/errors/slices", params={"score_id": s["score_id"], "label": "speaker"}).json()["rows"][0]
    saved = client.post(f"/api/datasets/{asr.dataset_id}/slices",
                        json={"name": "one speaker", "filters": [{"key": "speaker", "value": first["value"]}]}).json()
    table = client.get("/api/benchmarks/asr").json()
    assert [x["name"] for x in table["slices"]] == ["one speaker"]
    vals = [c["slices"][str(saved["id"])] for c in table["cells"] if c.get("slices")]
    assert vals and all(v["n"] == first["n"] for v in vals)
    unit = client.get("/api/errors/slices/units", params={"score_id": s["score_id"],
                                                          "filters": json.dumps(saved["filters"])}).json()
    assert unit["n"] == first["n"] and unit["value"] == pytest.approx(first["score"]) and unit["worst"]


def test_diarisation_regions_and_matrices(flow, client, session):
    seg = next(r for r in session.scalars(select(Run).where(Run.task == "seg")) if r.model.has_speakers)
    ds = client.get(f"/api/errors/diar/summary?run_id={seg.id}").json()
    rows = {r["key"]: r for r in ds["regions"]["rows"]}
    assert set(rows) == {"overlap", "near_change", "short", "nonspeech"}
    assert all(r["share_errors"] is None or 0 <= r["share_errors"] <= 1 for r in rows.values())
    assert ds["speaker_counts"]["files"] == 6
    files = client.get(f"/api/errors/diar/files?score_id={ds['score_id']}").json()["rows"]
    fd = client.get(f"/api/errors/diar/files/{files[0]['id']}").json()
    assert fd["speaker_matrix"]["rows"] and all("context" in e for e in fd["errors"])


def test_pipeline_stage_cards_waterfall_and_causes(flow, client, session):
    checked = 0
    for pipe in session.scalars(select(Run).where(Run.task == "pipeline")):
        ps = client.get(f"/api/errors/pipeline/summary?run_id={pipe.id}").json()
        assert [s["kind"] for s in ps["stages"]] == ["seg", "asr", "output"]
        if not ps["has_speakers"] or ps["oracle"]:
            continue
        wf = ps["waterfall"]
        att = ps["tiles"]["attribution"]
        assert [w["key"] for w in wf][:3] == ["asr", "segmentation", "speaker"]
        assert wf[2]["total"] == pytest.approx(att["cpwer"])
        assert wf[-1]["key"] == "timing" and wf[-1]["total"] == pytest.approx(ps["tiles"]["tcpwer"])
        rows = client.get(f"/api/errors/pipeline/files?score_id={ps['score_id']}").json()["rows"]
        pf = client.get(f"/api/errors/pipeline/files/{rows[0]['id']}").json()
        tagged = sum(1 for line in pf["ref_lines"] for w in line["words"] if w["cause"])
        wrong = sum(p["s"] + p["d"] for p in pf["pairs"])
        assert tagged == wrong
        checked += 1
    assert checked


def test_enhancement_on_the_replayed_fixtures(flow, client, session):
    """The identity enhancer's copy has the originals' audio, so enhanced runs replay and change nothing."""
    from concord_bench.acceptance import run_enhancement_flow
    from concord_bench.core.models import EnhancedCopy

    out = run_enhancement_flow(client, flow["dataset"]["id"], log=lambda m: None)
    assert [c["status"] for c in out["copies"]] == ["ready"] and out["copies"][0]["length_check"] == "ok"
    enh = [c for c in out["asr"]["cells"] if c["enh_model_id"]]
    assert len(enh) == 2 and all(c["vs_original"] == pytest.approx(0.0) for c in enh)
    rows = [r for r in out["pipelines"]["leaderboard"] if r["enh_model_id"]]
    assert {r["placement"] for r in rows} == {"before", "after"}
    assert all(r["vs_original"] == pytest.approx(0.0) for r in rows)
    copy = session.query(EnhancedCopy).one()
    clips = client.get("/api/errors/enh/clips", params={"run_id": rows[0]["run_id"], "base_run_id": next(
        r["run_id"] for r in out["pipelines"]["leaderboard"] if r["enh_model_id"] is None and
        r["seg_model_id"] == rows[0]["seg_model_id"] and r["asr_model_id"] == rows[0]["asr_model_id"])}).json()
    assert clips["copy_id"] == copy.id and clips["clips"] == []  # nothing changed
    models = {m["catalog_id"]: m for m in client.get("/api/models").json()["models"]}
    assert models["identity-enhancer"]["runs"] == 2 + len(rows)  # the runs that read its copy
