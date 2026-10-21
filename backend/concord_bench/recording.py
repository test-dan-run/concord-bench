"""`just record`: run the acceptance flow against live services and record every model response.

Writes `fixtures/recorded/<service>/<key>.json`, a copy of the endpoint map the responses were taken
with (`endpoints.generated.yaml`: replay keys include image tags, so tests pin this copy), and
`summary.json` with the scores of the recorded run for regression checks.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .core.settings import REPO_ROOT, reset_settings


def record(out: str, services: list[str] | None = None, extra_checks: list[str] | None = None) -> int:
    import aicore
    from fastapi.testclient import TestClient

    out_dir = (REPO_ROOT / out).resolve() if not Path(out).is_absolute() else Path(out)
    work = Path(tempfile.mkdtemp(prefix="concord-record-"))
    os.environ.update({"CONCORD_DATA": str(work / "data"), "RECORD_DIR": str(out_dir)})
    os.environ.pop("REPLAY_DIR", None)
    reset_settings()
    from .core import db

    db.reset_engines()
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(aicore.endpoints_path(), out_dir / "endpoints.generated.yaml")

    from . import acceptance
    from .api.app import create_app

    asr = [s for s in acceptance.ASR_MODELS if not services or s in services]
    seg = [s for s in acceptance.SEG_MODELS if not services or s in services]
    with TestClient(create_app()) as client:
        res = acceptance.run_flow(client, work / "staging", asr, seg, extra_checks=extra_checks)
    summary = {
        "checks": {k: {"capabilities": v["capabilities"], "timing_level": v["timing_level"],
                       "lines": [(ln["status"], ln["name"]) for ln in v["lines"]]} for k, v in res["checks"].items()},
        "asr": {c["model_id"]: c["value"] for c in res["asr"]["cells"]},
        "diar": {c["model_id"]: [c["metric"], c["value"]] for c in res["diar"]["cells"]},
        "pipelines": [{"label": c["label"], "cpwer": c["cpwer"], "wer": c["wer"], "der": c["der"]}
                      for c in res["pipelines"]["runs"]],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(f"recorded into {out_dir} (work dir {work})")
    return 0
