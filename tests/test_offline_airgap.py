"""Air-gap test of an offline kit (OFFLINE_EXPORT_PLAN.md §8), `just test-offline`.

Exports a full kit with `silero-vad` (CPU only: a 434 MB image and 2.3 MB of weights, no GPU), installs
it with `install.sh --internal` on a Docker network with no outside access, registers the model through
the installed Concord's API, runs a VAD benchmark on the fixture set and compares the score with the
one recorded on the source machine (fixtures/recorded/summary.json).

The kit runs on this machine's Docker daemon next to the source platform, so its config.yaml is given
its own network and compose project after the checksums are verified (README: config.yaml can be
edited on the offline machine). Nothing of the source platform is started or stopped.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from concord.acceptance import stage_fixture_dataset
from concord.offline import export, kit

pytestmark = pytest.mark.offline

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "concord-lite:0.1.0"
SERVICE = "silero-vad"
NETWORK = "concord-airgap-net"
PROJECT = "concord-airgap"  # the kit platform's compose project
CONCORD_PROJECT = "concord-airgap-app"
PORT = "8613"


def docker(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True, **kw)


def recorded_score() -> float:
    summary = json.loads((ROOT / "fixtures" / "recorded" / "summary.json").read_text())
    values = [v[1] for v in summary["diar"].values() if v[0] == "VAD error"]
    assert len(values) == 1, summary["diar"]
    return values[0]


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture
def kit_dir(tmp_path_factory):
    if shutil.which("docker") is None or docker("info", check=False).returncode != 0:
        pytest.skip("needs Docker")
    for ref in (IMAGE, "ghcr.io/owner/silero-vad:0.1.0"):
        if docker("image", "inspect", ref, check=False).returncode != 0:
            pytest.skip(f"needs the image {ref}")
    if not port_free(8401):
        pytest.skip("port 8401 is in use (the source platform's silero-vad?); stop it first")
    dest = tmp_path_factory.mktemp("airgap")
    yield dest
    # teardown: the kit's services, Concord, the network, then the root-owned files containers wrote
    ids = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}", check=False).stdout.split()
    ids += docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={CONCORD_PROJECT}", check=False).stdout.split()
    if ids:
        docker("rm", "-f", *ids, check=False)
    docker("volume", "rm", f"{CONCORD_PROJECT}_data", check=False)
    docker("network", "rm", NETWORK, check=False)
    docker("run", "--rm", "-v", f"{dest}:/w", "--entrypoint", "sh", IMAGE, "-c", "rm -rf /w/* /w/.[!.]*", check=False)


def test_full_kit_installs_and_benchmarks_on_an_internal_network(kit_dir, data_dir):
    lines: list[str] = []
    out = export.export([SERVICE], "full", kit_dir, IMAGE, log=lines.append)
    k = Path(out["path"])
    assert kit.verify(k) == []

    # The offline machine's admin step: this kit gets its own network and compose project.
    cfg = k / "platform" / "config.yaml"
    cfg.write_text(cfg.read_text().replace("network: ai-net", f"network: {NETWORK}\ncompose_project: {PROJECT}"))
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "CONCORD_PORT": PORT,
           "CONCORD_PROJECT": CONCORD_PROJECT}
    r = subprocess.run([str(k / "install.sh"), "--internal", "--skip-verify"], env=env, capture_output=True, text=True,
                       timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    net = json.loads(docker("network", "inspect", NETWORK).stdout)[0]
    assert net["Internal"] is True

    container = f"{CONCORD_PROJECT}-concord-1"
    deadline = time.monotonic() + 120
    probe = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8600/api/config', timeout=3)"
    while docker("exec", container, "python", "-c", probe, check=False).returncode != 0:
        assert time.monotonic() < deadline, docker("logs", container, check=False).stdout[-3000:]
        time.sleep(2)

    staging = stage_fixture_dataset(kit_dir / "staging")
    docker("cp", str(staging), f"{container}:/tmp/fixture")
    docker("cp", str(ROOT / "tests" / "airgap_flow.py"), f"{container}:/tmp/airgap_flow.py")
    flow = docker("exec", container, "python", "/tmp/airgap_flow.py", "/tmp/fixture", SERVICE, check=False, timeout=1800)
    assert flow.returncode == 0, flow.stdout + flow.stderr
    res = json.loads(flow.stdout.strip().splitlines()[-1])

    assert res["internet"] is False  # the Concord container has no way out
    assert res["registry"] == [SERVICE]  # the kit's platform holds only what was exported
    assert res["check"]["ok"] and res["check"]["capabilities"] == ["vad"], res["check"]["log"]
    started = docker("ps", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format", "{{.Names}} {{.Networks}}")
    assert started.stdout.split() == [f"{PROJECT}-{SERVICE}-1", NETWORK]
    mounts = json.loads(docker("inspect", f"{PROJECT}-{SERVICE}-1").stdout)[0]["Mounts"]
    assert [m["Source"] for m in mounts] == [str(k / "assets" / "custom-models" / "silero-vad" / "v6.2.3")]

    assert res["score"]["metric"] == "VAD error"
    assert res["score"]["value"] == pytest.approx(recorded_score(), abs=1e-6), res

    # A model kit merged in with install.sh --add (the merge runs in the installed Concord image).
    mk = Path(export.export(["text-normalizer"], "models", kit_dir, log=lines.append)["path"])
    r = subprocess.run([str(mk / "install.sh"), "--add", str(k)], env=env, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "add: text-normalizer" in r.stdout
    read = "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8600/api/registry').read().decode())"
    registry = json.loads(docker("exec", container, "python", "-c", read).stdout)
    assert sorted(e["name"] for e in registry) == [SERVICE, "text-normalizer"]  # Concord sees the re-rendered map
    owner = k.stat().st_uid
    assert [f for f in (k / "platform").rglob("*") if f.lstat().st_uid != owner] == []
    # the same service with another image is refused, and the install keeps what it has
    svc = mk / "platform" / "services" / "text-normalizer.yaml"
    svc.write_text(svc.read_text().replace("text-normalizer:0.1.0", "text-normalizer:0.2.0"))
    before = (k / "platform" / "services" / "text-normalizer.yaml").read_text()
    r = subprocess.run([str(mk / "install.sh"), "--add", str(k), "--skip-verify"], env=env, capture_output=True,
                       text=True, timeout=300)
    assert r.returncode == 1 and "the install has it with image" in r.stderr, r.stdout + r.stderr
    assert (k / "platform" / "services" / "text-normalizer.yaml").read_text() == before
    print(f"\nair-gap VAD error {res['score']['value']:.6f}, source machine {recorded_score():.6f}; kit {out['bytes'] / 1e9:.2f} GB")
