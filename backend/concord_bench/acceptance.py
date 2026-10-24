"""The scripted Phase 1 acceptance run (HANDOFF §3.7), through the HTTP API.

1. Upload the labelled fixture dataset (audio + speaker-attributed STM) by local path.
2. Pick >= 2 ASR and >= 2 VAD / diarisation models from the model catalog, typing no fields (the catalog
   has registered and verified them; Bench only lists them).
3. Run single-model benchmarks and the full pipeline grid incl. the oracle, stage by stage, after
   previewing the catalog's plan.

Used by `just record` (live catalog and services, responses recorded) and by the end-to-end test
(replayed, with the replay stand-in for the catalog). Jobs run in this process via `jobs.run_pending()`,
so no separate worker is needed.
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
             log: Callable[[str], None] = print) -> dict[str, Any]:
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

    # 2. models: listed by the catalog with what its verifier found; nothing to type, nothing to register here
    listing = _ok(client.get("/api/models"))
    by_id = {m["catalog_id"]: m for m in listing["models"]}
    out["models"] = {}
    for name in seg_models + asr_models:
        m = by_id.get(name)
        if m is None or not m["usable"]:
            raise AssertionError(f"{name} is not a usable catalog model: {m and m['not_usable']}")
        out["models"][name] = m
        log(f"model {name}: {m['capabilities']} timing={m['timing_level']} build {m['build_key']}")

    # 3. benchmarks: each stage alone, then the full grid incl. the oracle (previewed first)
    stages = {"dataset_id": ds_id, "seg_models": seg_models, "asr_models": asr_models, "mode": "stages"}
    grid = {"dataset_id": ds_id, "seg_models": seg_models, "asr_models": asr_models, "oracle": True, "mode": "grid"}
    out["plan"] = _ok(client.post("/api/benchmarks/plan/preview", json=grid))
    _ok(client.post("/api/benchmarks/plan", json=stages), 202)
    _ok(client.post("/api/benchmarks/plan", json=grid), 202)
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
