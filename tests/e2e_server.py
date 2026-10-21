"""Server for the Playwright smoke tests: a fresh data dir populated by replaying the acceptance flow
on recorded responses (plus a pseudo-labelled audio-only dataset for the Label screens), then the API +
built frontend on --port. No GPU or platform needed.

    uv run python tests/e2e_server.py --port 8611
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8611)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="concord-e2e-"))
    rec = ROOT / "fixtures" / "recorded"
    os.environ.update({"CONCORD_DATA": str(work / "data"), "REPLAY_DIR": str(rec),
                       "AICORE_ENDPOINTS": str(rec / "endpoints.generated.yaml"), "FEATURE_IMPROVE": "0"})
    sys.path.insert(0, str(ROOT / "backend"))
    import uvicorn
    from fastapi.testclient import TestClient

    from concord_bench.acceptance import run_flow, run_label_flow
    from concord_bench.api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        flow = run_flow(client, work / "staging", log=lambda m: print(m, flush=True))
        run_label_flow(client, work / "staging-label", flow, log=lambda m: print(m, flush=True))
    # Jobs started from the browser (e.g. re-fusing pseudo-labels) need a worker, as under `just dev`.
    from concord_bench.runs import jobs

    def worker() -> None:
        while True:
            if jobs.run_pending(max_jobs=1) == 0:
                time.sleep(0.5)

    threading.Thread(target=worker, daemon=True).start()
    print(f"e2e data ready in {work}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
