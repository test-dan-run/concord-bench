"""Helpers shared by the Label tests (fixtures `label_flow` / `labelled` are in conftest.py)."""

from __future__ import annotations

import shutil

from concord_bench.acceptance import FIXTURES
from concord_bench.runs import jobs


def _ok(r, *codes):
    assert r.status_code in (codes or (200,)), f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text}"
    return r.json() if r.content else None


def audio_only_dataset(client, tmp_path, name="Unlabelled calls") -> int:
    src = tmp_path / f"audio-only-{name.replace(' ', '-')}"
    shutil.copytree(FIXTURES / "dataset" / "audio", src / "audio")
    ds = _ok(client.post("/api/datasets", json={"name": name, "language": "en", "tags": ["call-centre"],
                                                "local_path": str(src)}), 201)["dataset"]
    jobs.run_pending()
    d = _ok(client.get(f"/api/datasets/{ds['id']}"))
    assert d["status"] == "ready" and d["mode"] == "audio_only"
    return d["id"]


def label(client, flow, **kw):
    ids = flow["ids"]
    body = {"tasks": ["asr", "diar"], "seg_model_id": ids["pyannote-diarization"],
            "asr_model_ids": [ids["whisper-large-v3"], ids["faster-whisper-large-v3"]], **kw}
    started = _ok(client.post(f"/api/label/datasets/{flow['unlabelled']}/runs", json=body), 202)
    jobs.run_pending()
    job = _ok(client.get(f"/api/jobs/{started['job']['id']}"))
    assert job["status"] == "done", job["error"]
    return started, _ok(client.get(f"/api/label/datasets/{flow['unlabelled']}/results"))
