"""Runs inside the Concord container of an installed offline kit (tests/test_offline_airgap.py).

    python airgap_flow.py DATASET_DIR SERVICE

Drives the running API on 127.0.0.1:8600 the way a user would: upload the fixture dataset by local
path, find SERVICE among the model catalog's models (the kit's catalog has registered and verified it),
run a VAD / diarisation benchmark (the worker leases it, which starts it) and print the score as JSON.
Standard library only. It also checks that the container cannot reach the internet. Needs a model
catalog in the kit (CATALOG_URL), which offline kits do not carry yet.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8600/api"


def call(method: str, path: str, body: dict | None = None) -> dict | list:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read()[:500]!r}") from None


def wait_job(job_id: int, timeout: float = 900) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        j = call("GET", f"/jobs/{job_id}")
        if j["status"] in ("done", "failed", "blocked"):
            if j["status"] != "done":
                raise SystemExit(f"job {job_id} ({j['label']}) {j['status']}: {j['error']}\n" + "\n".join(j["log"][-25:]))
            return j
        time.sleep(1)
    raise SystemExit(f"job {job_id} did not finish in {timeout}s")


def internet_reachable() -> bool:
    try:
        socket.create_connection(("huggingface.co", 443), timeout=5).close()
        return True
    except OSError:
        return False


def main(dataset: str, service: str) -> None:
    out: dict = {"internet": internet_reachable()}
    created = call("POST", "/datasets", {"name": "Synth fixtures", "language": "en", "tags": ["synthetic"],
                                         "local_path": dataset})
    wait_job(created["job"]["id"])
    ds = call("GET", f"/datasets/{created['dataset']['id']}")
    out["dataset"] = {"files": ds["files"], "usable_files": ds["usable_files"], "label_types": ds["label_types"]}

    listing = call("GET", "/models")
    out["models"] = [m["catalog_id"] for m in listing["models"]]
    model = next((m for m in listing["models"] if m["catalog_id"] == service), None)
    if model is None:
        raise SystemExit(f"{service} is not among the catalog's models: {out['models']} ({listing['catalog']})")
    out["check"] = {"capabilities": model["capabilities"], "ok": model["usable"], "log": [model["not_usable"] or ""]}

    call("POST", "/benchmarks/plan", {"dataset_id": ds["id"], "seg_models": [service], "mode": "stages"})
    for j in call("GET", "/jobs?limit=50"):
        if j["status"] in ("queued", "waiting", "running"):
            wait_job(j["id"])
    cells = [c for c in call("GET", "/benchmarks/diar")["cells"] if c["dataset_id"] == ds["id"]]
    out["score"] = {"metric": cells[0]["metric"], "value": cells[0]["value"], "image": cells[0].get("image")}
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
