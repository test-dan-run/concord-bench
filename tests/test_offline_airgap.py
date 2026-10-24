"""Air-gap test of an offline kit (SPLIT_PLAN.md §7), `just test-offline`.

Exports a full kit with `silero-vad` (CPU only: a 434 MB image and 2.3 MB of weights, no GPU), installs
it with `install.sh --internal` on a Docker network with no outside access, finds the model among the
model catalog's models through the installed Concord Bench's API, runs a VAD benchmark on the fixture set
and compares the score with the one recorded on the source machine (fixtures/recorded/summary.json).
Kits carry no model catalog yet, so this fails until they do (see README, Offline kits).

Two variants: a kit with Concord Bench only (which also merges a model kit with `install.sh --add`), and
a kit with both app images (CONTRACTS.md §6), where each app must also reach the other by its compose
hostname with the same contract version.

The kit runs on this machine's Docker daemon next to the source platform, so its config.yaml is given
its own network and compose project after the checksums are verified (README: config.yaml can be
edited on the offline machine). Nothing of the source platform is started or stopped. The apps publish
their contract ports (8600, 8620) on 127.0.0.1, so those must be free.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from concord_bench.acceptance import stage_fixture_dataset
from concord_bench.offline import export, kit

pytestmark = pytest.mark.offline

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {"concord-bench": "concord-bench:0.2.0", "concord-label": "concord-label:0.2.0"}
PORTS = {"concord-bench": 8600, "concord-label": 8620}
SERVICE = "silero-vad"
NETWORK = "concord-airgap-net"
PROJECT = "concord-airgap"  # the kit platform's compose project
CONCORD_PROJECT = "concord-airgap-app"


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


def container(app: str) -> str:
    return f"{CONCORD_PROJECT}-{app}-1"


def get_json(app: str, url: str) -> dict | list:
    """GET url from inside the app's container (standard library only)."""
    read = f"import sys, urllib.request; sys.stdout.write(urllib.request.urlopen({url!r}, timeout=10).read().decode())"
    return json.loads(docker("exec", container(app), "python", "-c", read).stdout)


def wait_api(app: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    probe = f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{PORTS[app]}/api/config', timeout=3)"
    while docker("exec", container(app), "python", "-c", probe, check=False).returncode != 0:
        assert time.monotonic() < deadline, docker("logs", container(app), check=False).stdout[-3000:]
        time.sleep(2)


@pytest.fixture
def kit_dir(tmp_path_factory):
    if shutil.which("docker") is None or docker("info", check=False).returncode != 0:
        pytest.skip("needs Docker")
    for ref in (IMAGES["concord-bench"], "ghcr.io/owner/silero-vad:0.1.0"):
        if docker("image", "inspect", ref, check=False).returncode != 0:
            pytest.skip(f"needs the image {ref}")
    for port in (8401, *PORTS.values()):
        if not port_free(port):
            pytest.skip(f"port {port} is in use (the source platform's silero-vad, or an app in development?); stop it first")
    dest = tmp_path_factory.mktemp("airgap")
    yield dest
    # teardown: the kit's services, the apps, their volumes, the network, then the root-owned files containers wrote
    ids = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}", check=False).stdout.split()
    ids += docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={CONCORD_PROJECT}", check=False).stdout.split()
    if ids:
        docker("rm", "-f", *ids, check=False)
    docker("volume", "rm", f"{CONCORD_PROJECT}_bench-data", f"{CONCORD_PROJECT}_label-data", check=False)
    docker("network", "rm", NETWORK, check=False)
    docker("run", "--rm", "-v", f"{dest}:/w", "--entrypoint", "sh", IMAGES["concord-bench"], "-c", "rm -rf /w/* /w/.[!.]*",
           check=False)


@pytest.mark.parametrize("apps", [("concord-bench",), ("concord-bench", "concord-label")], ids=["bench", "bench+label"])
def test_full_kit_installs_and_benchmarks_on_an_internal_network(kit_dir, data_dir, apps):
    for app in apps:
        if docker("image", "inspect", IMAGES[app], check=False).returncode != 0:
            pytest.skip(f"needs the image {IMAGES[app]} (`just build` in that repository)")
    lines: list[str] = []
    out = export.export([SERVICE], "full", kit_dir, {a: IMAGES[a] for a in apps}, log=lines.append)
    k = Path(out["path"])
    assert kit.verify(k) == []
    assert [a["app"] for a in out["manifest"]["apps"]] == list(apps)
    assert sorted(p.name for p in (k / "images").iterdir()) == sorted([f"{a}.tar" for a in apps] + ["platform.tar"])

    # The offline machine's admin step: this kit gets its own network and compose project.
    cfg = k / "platform" / "config.yaml"
    cfg.write_text(cfg.read_text().replace("network: ai-net", f"network: {NETWORK}\ncompose_project: {PROJECT}"))
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "CONCORD_PROJECT": CONCORD_PROJECT}
    r = subprocess.run([str(k / "install.sh"), "--internal", "--skip-verify"], env=env, capture_output=True, text=True,
                       timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    for app in apps:
        assert f"http://127.0.0.1:{PORTS[app]}" in r.stdout
    net = json.loads(docker("network", "inspect", NETWORK).stdout)[0]
    assert net["Internal"] is True
    running = docker("ps", "--filter", f"label=com.docker.compose.project={CONCORD_PROJECT}", "--format", "{{.Names}}")
    assert sorted(running.stdout.split()) == sorted(container(a) for a in apps)

    for app in apps:
        wait_api(app)
    for app in apps:
        cfg_json = get_json(app, f"http://127.0.0.1:{PORTS[app]}/api/config")
        assert cfg_json["app"] == app and cfg_json["contract_version"] == 1
        if len(apps) == 2:  # each app reaches the other by its compose hostname, same contract version
            other = next(a for a in apps if a != app)
            # an app that asked before its peer was up keeps that answer for 30 s
            deadline = time.monotonic() + 45
            while not cfg_json["peer"]["reachable"] and time.monotonic() < deadline:
                time.sleep(3)
                cfg_json = get_json(app, f"http://127.0.0.1:{PORTS[app]}/api/config")
            assert cfg_json["peer"] == {"url": f"http://{other}:{PORTS[other]}", "reachable": True, "contract_version": 1}
        else:
            assert cfg_json["peer"]["url"] is None

    bench = container("concord-bench")
    staging = stage_fixture_dataset(kit_dir / "staging")
    docker("cp", str(staging), f"{bench}:/tmp/fixture")
    docker("cp", str(ROOT / "tests" / "airgap_flow.py"), f"{bench}:/tmp/airgap_flow.py")
    flow = docker("exec", bench, "python", "/tmp/airgap_flow.py", "/tmp/fixture", SERVICE, check=False, timeout=1800)
    assert flow.returncode == 0, flow.stdout + flow.stderr
    res = json.loads(flow.stdout.strip().splitlines()[-1])

    assert res["internet"] is False  # the Concord container has no way out
    assert res["models"] == [SERVICE]  # the kit's platform (and its catalog) holds only what was exported
    assert res["check"]["ok"] and res["check"]["capabilities"] == ["vad"], res["check"]["log"]
    started = docker("ps", "--filter", f"label=com.docker.compose.project={PROJECT}", "--format", "{{.Names}} {{.Networks}}")
    assert started.stdout.split() == [f"{PROJECT}-{SERVICE}-1", NETWORK]
    mounts = json.loads(docker("inspect", f"{PROJECT}-{SERVICE}-1").stdout)[0]["Mounts"]
    assert [m["Source"] for m in mounts] == [str(k / "assets" / "custom-models" / "silero-vad" / "v6.2.3")]

    assert res["score"]["metric"] == "VAD error"
    assert res["score"]["value"] == pytest.approx(recorded_score(), abs=1e-6), res
    print(f"\nair-gap ({'+'.join(apps)}) VAD error {res['score']['value']:.6f}, source machine {recorded_score():.6f}; "
          f"kit {out['bytes'] / 1e9:.2f} GB")
    if len(apps) == 2:
        return

    # A model kit merged in with install.sh --add (the merge runs in the installed app image).
    mk = Path(export.export(["text-normalizer"], "models", kit_dir, log=lines.append)["path"])
    r = subprocess.run([str(mk / "install.sh"), "--add", str(k)], env=env, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "add: text-normalizer" in r.stdout
    normalisers = get_json("concord-bench", "http://127.0.0.1:8600/api/normalisers")
    assert "platform:text-normalizer" in [n["id"] for n in normalisers]  # the catalog sees the re-rendered platform
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
