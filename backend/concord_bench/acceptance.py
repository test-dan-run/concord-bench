"""The scripted Phase 1 acceptance run (HANDOFF §3.7), through the HTTP API.

1. Upload the labelled fixture dataset (audio + speaker-attributed STM) by local path.
2. Register >= 2 ASR and >= 2 VAD / diarisation models from the registry, typing no fields.
3. Run single-model benchmarks and the full pipeline grid incl. the oracle.

Used by `just record` (live services, responses recorded) and by the end-to-end test (replayed).
Jobs run in this process via `jobs.run_pending()`, so no separate worker is needed.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .core.settings import REPO_ROOT
from .runs import jobs

FIXTURES = REPO_ROOT / "fixtures" / "generated"
ASR_MODELS = ["whisper-large-v3", "faster-whisper-large-v3"]
SEG_MODELS = ["silero-vad", "pyannote-diarization", "nemotron-3-diarization"]


def stage_fixture_dataset(dest: Path, refs: str = "stm") -> Path:
    """Copy the fixture audio plus one reference format into a fresh folder."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(FIXTURES / "dataset" / "audio", dest / "audio")
    shutil.copytree(FIXTURES / "refs" / refs, dest / "refs")
    return dest


def _ok(r, *codes: int) -> Any:
    if r.status_code not in (codes or (200,)):
        raise AssertionError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:500]}")
    return r.json() if r.content else None


def run_flow(client, staging: Path, asr_models: list[str] | None = None, seg_models: list[str] | None = None,
             extra_checks: list[str] | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    asr_models = asr_models or ASR_MODELS
    seg_models = seg_models or SEG_MODELS
    out: dict[str, Any] = {"typed_fields": 0}

    # 1. dataset
    stage_fixture_dataset(staging)
    created = _ok(client.post("/api/datasets", json={
        "name": "Synth fixtures", "language": "en", "tags": ["synthetic", "en", "call-centre"],
        "local_path": str(staging)}), 201)
    ds_id = created["dataset"]["id"]
    jobs.run_pending()
    ds = _ok(client.get(f"/api/datasets/{ds_id}"))
    assert ds["status"] == "ready", ds
    out["dataset"] = ds
    log(f"dataset {ds_id}: {ds['files']} files, {ds['usable_files']} usable, labels {ds['label_types']}")

    # 2. models: pick from the registry, run the check, add with no typed fields
    out["models"] = {}
    out["checks"] = {}
    for name in seg_models + asr_models + (extra_checks or []):
        job = _ok(client.post(f"/api/registry/{name}/check"), 202)
        jobs.run_pending()
        job = _ok(client.get(f"/api/jobs/{job['id']}"))
        if job["status"] != "done":
            raise AssertionError(f"check of {name} {job['status']}: {job['error']}\n" + "\n".join(job["log"][-15:]))
        report = job["result"]["report"]
        out["checks"][name] = report
        log(f"check {name}: {report['capabilities']} timing={report['timing_level']}")
        if name in (extra_checks or []):
            continue
        m = _ok(client.post("/api/models", json={"registry_name": name, "check_job_id": job["id"]}), 201)
        out["models"][name] = m

    # 3. benchmarks: each stage alone, then the full grid incl. the oracle
    seg_ids = [out["models"][n]["id"] for n in seg_models]
    asr_ids = [out["models"][n]["id"] for n in asr_models]
    _ok(client.post("/api/benchmarks/plan", json={"dataset_id": ds_id, "seg_model_ids": seg_ids,
                                                  "asr_model_ids": asr_ids, "mode": "stages"}), 202)
    _ok(client.post("/api/benchmarks/plan", json={"dataset_id": ds_id, "seg_model_ids": seg_ids,
                                                  "asr_model_ids": asr_ids, "oracle": True, "mode": "grid"}), 202)
    jobs.run_pending()
    failed = [j for j in _ok(client.get("/api/jobs?limit=200")) if j["status"] in ("failed", "blocked")]
    if failed:
        raise AssertionError("jobs failed: " + "; ".join(f"{j['label']}: {j['error']}" for j in failed))
    out["asr"] = _ok(client.get("/api/benchmarks/asr"))
    out["diar"] = _ok(client.get("/api/benchmarks/diar"))
    out["pipelines"] = _ok(client.get(f"/api/benchmarks-pipelines?dataset_id={ds_id}"))
    for c in out["asr"]["cells"]:
        if c["dataset_id"] == ds_id:
            log(f"ASR  model {c['model_id']}: WER {c['value']:.3f}  RTF {c['rtf']:.3f}")
    for c in out["diar"]["cells"]:
        if c["dataset_id"] == ds_id:
            log(f"SEG  model {c['model_id']}: {c['metric']} {c['value']:.3f}")
    for c in out["pipelines"]["runs"]:
        cp = f"{c['cpwer']:.3f}" if c["cpwer"] is not None else "n/a"
        log(f"PIPE {c['label']}: cpWER {cp}  WER {c['wer']:.3f}  RTF {c['rtf']:.3f}")
    return out
