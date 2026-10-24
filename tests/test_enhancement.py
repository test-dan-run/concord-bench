"""EVALUATE_PLAN.md E3: speech enhancement as an ablation. Enhanced copies (chunking, the length check,
cancel and delete), runs and pipelines that read a copy before or after diarisation, plans with enhancers,
and the leaderboards' enhancer rows. Models are fakes behind aicore's FakeCatalog."""

from __future__ import annotations

import json
import wave

import numpy as np
import pytest
from aicore.catalog import FakeCatalog
from sqlalchemy import select

from concord_bench import adapters
from concord_bench.adapters.base import Adapter
from concord_bench.adapters.enhance import OIPEnhancer
from concord_bench.api.benchmarks import enqueue_plan, plan_size
from concord_bench.core import paths
from concord_bench.core.models import AudioFile, Dataset, EnhancedCopy, EnhancedFile, Job, Model, ModelOutput, Run
from concord_bench.datasets.ingest import ingest
from concord_bench.platform import catalog
from concord_bench.runs import enhance, jobs
from conftest import catalog_model

SR = 16000


def _tone(path, seconds, amp=0.1):
    t = np.arange(int(seconds * SR)) / SR
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((amp * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes())


class FakeASR(Adapter):
    """Hears quiet audio (an enhanced copy is half as loud) as one word fewer."""
    kind, contract, api = "asr", "openai", "audio.transcriptions"
    calls = 0

    def call(self, audio):
        FakeASR.calls += 1
        loud = float(np.max(np.abs(audio))) if len(audio) else 0.0
        return {"text": "hello there world" if loud > 0.07 else "hello world"}

    def parse(self, raw, duration):
        return {"text": raw["text"], "words": None, "segments": None}


class FakeSeg(Adapter):
    kind, contract, api = "seg", "oip-v2", "infer"

    def call(self, audio):
        n = len(audio) / SR
        return {"SEGMENTS": [[0.0, n / 2], [n / 2, n]], "SPEAKERS": [0, 1]}

    def parse(self, raw, duration):
        return {"segments": raw["SEGMENTS"], "speakers": [f"spk{k}" for k in raw["SPEAKERS"]]}


def halve(audio, sample_rate=16000):
    return np.asarray(audio, dtype=np.float32) * 0.5, sample_rate


@pytest.fixture
def cat(data_dir, monkeypatch):
    fake = FakeCatalog([
        catalog_model("asr1", ["asr"], contract="openai", api="audio.transcriptions", gpu={"fraction": 0.2, "gb": 6.0}),
        catalog_model("diar1", ["diar"]),
        {**catalog_model("dfn", ["enhance"], build_key="e000000000000001", gpu=None),
         "capabilities": {"build_key": "e000000000000001", "status": "verified", "tasks": ["enhance"],
                          "usable_as": "enhancer", "report": {"sample": {"si_sdr_input_db": 5.0,
                                                                         "si_sdr_output_db": 12.3}}}},
    ])
    catalog.use(fake)
    real = adapters.for_model

    def for_model(model, language=None):
        if model.is_enh:
            return real(model, language)
        return FakeASR(model.catalog_id, {}) if model.is_asr else FakeSeg(model.catalog_id, {})

    monkeypatch.setattr(adapters, "for_model", for_model)
    monkeypatch.setattr(OIPEnhancer, "call", lambda self, audio, sample_rate=16000: halve(audio, sample_rate))
    FakeASR.calls = 0
    return fake


@pytest.fixture
def ds(session, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    lines = []
    for k in range(2):
        _tone(src / f"m{k}.wav", 2.0)
        lines += [{"file": f"m{k}", "start": 0.0, "end": 1.0, "speaker": "A", "text": "hello there"},
                  {"file": f"m{k}", "start": 1.0, "end": 2.0, "speaker": "B", "text": "world"}]
    (src / "segments.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    d = Dataset(name="meetings")
    session.add(d)
    session.flush()
    ingest(session, d.id, src)
    session.commit()
    return d


def _plan(session, ds, **kw):
    js = enqueue_plan(session, ds.id, {"seg_models": [], "asr_models": [], "oracle": False, "mode": "stages", **kw})
    jobs.run_pending()
    session.expire_all()
    return js


# ---------------------------------------------------------------- the audio

def test_length_within_10_ms_is_fixed_and_further_off_fails():
    x = np.ones(1000, dtype=np.float32)
    y, d = enhance.fix_length(np.ones(1005, dtype=np.float32), 1000)
    assert len(y) == 1000 and d == 5
    y, d = enhance.fix_length(np.ones(900, dtype=np.float32), 1000)
    assert len(y) == 1000 and d == 100 and y[-1] == 0
    with pytest.raises(enhance.LengthError, match="longer"):
        enhance.fix_length(np.ones(1000 + enhance.LENGTH_TOLERANCE + 1, dtype=np.float32), len(x))


def test_long_files_go_in_overlapping_chunks_joined_seamlessly(monkeypatch):
    monkeypatch.setattr(enhance, "WHOLE_MAX_S", 1.0)
    monkeypatch.setattr(enhance, "CHUNK_S", 0.5)
    monkeypatch.setattr(enhance, "OVERLAP_S", 0.1)
    n = int(2.3 * SR)
    spans = enhance.chunks(n)
    assert spans[0] == (0, 8000) and spans[-1][1] == n and len(spans) > 4
    assert all(b[0] == a[1] - 1600 for a, b in zip(spans, spans[1:], strict=False))  # 0.1 s overlap
    x = (0.3 * np.sin(np.arange(n) / 9)).astype(np.float32)
    calls = []

    class Same:
        def run(self, a):
            calls.append(len(a))
            return a.copy(), 0.01

    y, lat, shift = enhance.enhance_audio(Same(), x)
    assert len(y) == n and np.allclose(y, x, atol=1e-5)  # identity in, identity out: the crossfades sum to 1
    assert len(calls) == len(spans) and shift == 0 and lat == pytest.approx(0.01 * len(spans))
    assert enhance.chunks(SR) == [(0, SR)]  # short audio goes whole


# ---------------------------------------------------------------- copies

def test_a_copy_is_built_as_a_job_and_reused(session, cat, ds):
    from concord_bench.api.datasets import copies_of

    m = catalog.ensure_model(session, "dfn")
    c, job = jobs.copy_job(session, ds, m)
    session.commit()
    jobs.run_pending()
    session.expire_all()
    c = session.get(EnhancedCopy, c.id)
    assert c.status == "ready" and c.files == 2 and c.length_check == "ok" and c.max_shift_ms == 0
    assert c.build_key == "e000000000000001" and c.hardware["device"] == "cpu" and c.size_bytes > 0
    files = session.scalars(select(EnhancedFile).where(EnhancedFile.copy_id == c.id)).all()
    originals = {f.id: f.sha256 for f in session.scalars(select(AudioFile))}
    for e in files:
        assert e.sha256 != originals[e.audio_file_id] and paths.derived(e.sha256).exists()
        assert paths.peaks(e.sha256).exists() and e.duration == pytest.approx(2.0)
    assert jobs.copy_job(session, ds, m) == (c, None)  # ready: reused, nothing queued
    listing = copies_of(session, ds)
    assert [x["status"] for x in listing] == ["ready"] and listing[0]["display_name"] == "dfn"


def test_outputs_a_few_samples_off_are_trimmed_and_far_off_fail(session, cat, ds, monkeypatch):
    m = catalog.ensure_model(session, "dfn")
    monkeypatch.setattr(OIPEnhancer, "call", lambda self, a, sr=16000: (np.concatenate([a, np.zeros(80)]) * 0.5, sr))
    c, _ = jobs.copy_job(session, ds, m)
    session.commit()
    jobs.run_pending()
    session.expire_all()
    c = session.get(EnhancedCopy, c.id)
    assert c.status == "ready" and c.length_check == "trimmed" and c.max_shift_ms == pytest.approx(5.0)

    monkeypatch.setattr(OIPEnhancer, "call", lambda self, a, sr=16000: (np.concatenate([a, np.zeros(400)]), sr))
    enhance.delete(session, c)
    session.commit()
    c2, job = jobs.copy_job(session, ds, m)
    session.commit()
    jobs.run_pending()
    session.expire_all()
    c2 = session.get(EnhancedCopy, c2.id)
    assert c2.status == "failed" and "length check" in c2.error and "25.0 ms longer" in c2.error
    assert session.get(Job, job.id).status == "failed"


def test_cancel_a_queued_build_and_delete_a_ready_copy(session, cat, ds, client):
    m = catalog.ensure_model(session, "dfn")
    c, job = jobs.copy_job(session, ds, m)
    session.commit()
    r = client.delete(f"/api/enhanced/{c.id}").json()
    assert r["copy"]["status"] == "cancelled"
    session.expire_all()
    assert session.get(Job, job.id).status == "failed"
    built = client.post(f"/api/datasets/{ds.id}/enhanced", json={"models": ["dfn"]}).json()["copies"][0]
    assert built["copy"]["id"] != c.id and built["job"]
    jobs.run_pending()
    copy_id = built["copy"]["id"]
    f = session.scalars(select(AudioFile)).first()
    assert client.get(f"/api/audio/{f.id}", params={"copy_id": copy_id}).status_code == 200
    assert client.get(f"/api/audio/{f.id}/peaks", params={"copy_id": copy_id}).status_code == 200
    gone = client.delete(f"/api/enhanced/{copy_id}").json()
    assert gone["copy"]["status"] == "deleted" and gone["freed"] > 0
    assert client.get(f"/api/audio/{f.id}", params={"copy_id": copy_id}).status_code == 404
    assert client.post(f"/api/datasets/{ds.id}/enhanced", json={"models": ["asr1"]}).status_code == 422


# ---------------------------------------------------------------- runs on a copy

def test_runs_on_a_copy_read_its_audio_and_keep_scores_after_a_delete(session, cat, ds, client):
    _plan(session, ds, asr_models=["asr1"], enhancers=["dfn"])
    runs = {r.enh_model_id: r for r in session.scalars(select(Run).where(Run.task == "asr"))}
    m = session.scalar(select(Model).where(Model.catalog_id == "dfn"))
    plain, enh = runs[None], runs[m.id]
    assert plain.status == enh.status == "done" and enh.enh_copy_id is not None
    assert enh.builds["dfn"]["build_key"] == "e000000000000001" and enh.hardware["dfn"]["device"] == "cpu"
    assert "enh" in enh.rtf_by_stage and enh.rtf_by_stage["total"] == enh.rtf_by_stage["asr"]
    enhanced = {e.sha256 for e in session.scalars(select(EnhancedFile))}
    rows = session.scalars(select(ModelOutput).where(ModelOutput.model_name == "asr1")).all()
    assert {r.audio_sha256 for r in rows} >= enhanced  # the enhanced run's outputs are keyed by the copy's audio

    table = client.get("/api/benchmarks/asr").json()
    cells = [c for c in table["cells"] if c["dataset_id"] == ds.id]
    by_enh = {c["enh_model_id"]: c for c in cells}
    assert table["enhancers"][0]["catalog_id"] == "dfn"
    assert by_enh[m.id]["value"] != pytest.approx(by_enh[None]["value"])  # the quieter copy is heard differently
    assert by_enh[m.id]["vs_original"] == pytest.approx(by_enh[m.id]["value"] - by_enh[None]["value"])

    # a second run on the same copy calls no model
    calls = FakeASR.calls
    client.post("/api/runs", json={"task": "asr", "dataset_id": ds.id, "model_id": "asr1", "enh_model_id": "dfn"})
    jobs.run_pending()
    assert FakeASR.calls == calls

    # deleting the copy keeps the scores; a new run on it builds the copy again
    client.delete(f"/api/enhanced/{enh.enh_copy_id}")
    again = client.get("/api/benchmarks/asr").json()
    assert {c["enh_model_id"]: c["value"] for c in again["cells"] if c["dataset_id"] == ds.id}[m.id] == \
        pytest.approx(by_enh[m.id]["value"])
    r = client.post("/api/runs", json={"task": "asr", "dataset_id": ds.id, "model_id": "asr1",
                                       "enh_model_id": "dfn"}).json()
    assert r["job"]["after"]  # waits for the rebuilt copy
    jobs.run_pending()
    session.expire_all()
    assert session.get(Run, r["run"]["id"]).status == "done"


def test_plan_sizes():
    assert plan_size(2, 3, True, "grid") == 9
    assert plan_size(2, 3, True, "grid", enhancers=2, placements=2) == 2 * 3 * (1 + 4) + 3 * 3
    assert plan_size(2, 3, False, "grid", enhancers=1, include_original=False, placements=1) == 6
    assert plan_size(2, 3, False, "stages", enhancers=1) == 10


def test_pipelines_place_the_copy_before_or_after_diarisation(session, cat, ds, client):
    _plan(session, ds, seg_models=["diar1"], asr_models=["asr1"], oracle=True, mode="grid", enhancers=["dfn"],
          placements=["before", "after"])
    runs = session.scalars(select(Run).where(Run.task == "pipeline")).all()
    assert len(runs) == plan_size(1, 1, True, "grid", enhancers=1, placements=2) == 5
    assert all(r.status == "done" for r in runs), [(r.id, r.error) for r in runs]
    keyed = {(r.pipeline.seg_model_id is None, r.enh_model_id is not None, r.enh_placement) for r in runs}
    assert keyed == {(True, False, None), (False, False, None), (True, True, None), (False, True, "before"),
                     (False, True, "after")}
    seg_rows = session.scalars(select(ModelOutput).where(ModelOutput.model_name == "diar1")).all()
    originals = {f.sha256 for f in session.scalars(select(AudioFile))}
    enhanced = {e.sha256 for e in session.scalars(select(EnhancedFile))}
    seg_audio = {r.audio_sha256 for r in seg_rows}
    assert originals <= seg_audio and enhanced <= seg_audio  # "after" diarised the original, "before" the copy
    seg_stage_jobs = session.scalars(select(Job).where(Job.type == "seg_stage")).all()
    assert sorted(j.payload.get("enh_model_id") is not None for j in seg_stage_jobs) == [False, True]

    grid = client.get("/api/benchmarks-pipelines", params={"dataset_id": ds.id}).json()
    assert len(grid["cells"]) == 2 and all(c["score_id"] for c in grid["cells"])  # the grid is original audio
    lb = grid["leaderboard"]
    assert len(lb) == 5
    enhanced_rows = [x for x in lb if x["enh_model_id"] is not None]
    assert {x["placement"] for x in enhanced_rows} == {"before", "after", None}
    assert all("vs_original" in x for x in enhanced_rows)
    assert all(x["rtf_total"] >= (x["rtf"] or 0) for x in lb)
    labels = {x["label"] for x in enhanced_rows}
    assert any("before diarisation" in s for s in labels) and any("after diarisation" in s for s in labels)


def test_preview_lists_copies_to_build(session, cat, ds, client):
    pv = client.post("/api/benchmarks/plan/preview", json={
        "dataset_id": ds.id, "asr_models": ["asr1"], "seg_models": ["diar1"], "mode": "grid", "oracle": True,
        "enhancers": ["dfn"], "placements": ["before", "after"]}).json()
    assert pv["runs"] == 5 and pv["copies"] == [{"model": "dfn", "display_name": "dfn", "generative": False,
                                                  "status": "new", "progress": 0.0, "copy_id": None}]
    assert pv["plan"]["steps"][0]["stage"] == "enh"


def test_models_list_enhancers_with_their_check_clip(session, cat, client):
    models = {m["catalog_id"]: m for m in client.get("/api/models").json()["models"]}
    dfn = models["dfn"]
    assert dfn["stages"] == ["enh"] and dfn["capabilities"] == ["enhance"]
    assert dfn["check_sample"] == {"si_sdr_input_db": 5.0, "si_sdr_output_db": 12.3}
    assert models["asr1"]["check_sample"] is None


def test_an_upload_plan_queues_its_models(cat, client, tmp_path):
    """The upload page sends catalog ids (seg_models / asr_models); they used to be dropped."""
    src = tmp_path / "up"
    src.mkdir()
    _tone(src / "a.wav", 1.0)
    (src / "a.txt").write_text("hello there world")
    up = client.post("/api/uploads").json()["upload_id"]
    with open(src / "a.wav", "rb") as fa, open(src / "a.txt", "rb") as ft:
        client.post(f"/api/uploads/{up}/files", files=[("files", ("a.wav", fa)), ("files", ("a.txt", ft))])
    r = client.post("/api/datasets", json={"name": "up", "upload_id": up,
                                           "plan": {"asr_models": ["asr1"], "mode": "stages", "enhancers": ["dfn"]}})
    assert r.status_code == 201
    jobs.run_pending()
    runs = client.get("/api/runs").json()
    assert sorted((x["task"], x["enhancer"] or "", x["status"]) for x in runs) == [("asr", "", "done"),
                                                                                   ("asr", "dfn", "done")]
