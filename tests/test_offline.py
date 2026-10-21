"""Offline kits (unit): kit layout and manifest, checksum verification,
preview warnings, the export job and API, the `install.sh --add` merge rules on a fake install, and
install.sh itself against a fake `docker`. No real images or weights are copied; the platform's
bundle.py is replaced by a stand-in that writes what it would."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import types
from pathlib import Path

import pytest
import yaml

from concord_bench.offline import export, kit, merge

SILERO = {
    "name": "silero-vad", "alias": "default-vad", "kind": "model", "engine": "custom", "contract": "oip-v2",
    "api": "infer", "tasks": ["voice-activity-detection"], "description": "VAD", "port": 8401, "gpu": False,
    "gpu_fraction": 0.0, "image": "ghcr.io/owner/silero-vad:0.1.0", "image_present": True,
    "image_id": "sha256:aaa", "image_bytes": 434_000_000, "image_problem": None,
    "weights": {"kind": "custom", "source": "/a/custom-models/silero-vad/v6.2.3", "path": "custom-models/silero-vad/v6.2.3",
                "files": 1, "bytes": 2_327_524, "revision": None, "problems": []},
    "weights_bytes": 2_327_524, "problems": [],
}
WHISPER = {**SILERO, "name": "whisper-large-v3", "alias": "default-asr", "engine": "vllm", "contract": "openai",
           "api": "audio.transcriptions", "tasks": ["speech-recognition"], "port": 8101, "gpu": True,
           "gpu_fraction": 0.15, "image": "vllm/vllm-openai:v0.31.0", "image_id": "sha256:bbb",
           "image_bytes": 32_270_000_000,
           "weights": {**SILERO["weights"], "kind": "local", "path": "whisper-large-v3", "bytes": 3_090_000_000},
           "weights_bytes": 3_090_000_000}


def report(*rows, problems=(), warnings=()):
    rows = rows or (SILERO,)
    images = sorted({r["image"] for r in rows})
    return {
        "platform": {"gpu_total_gb": 32, "util_limit": 0.93, "network": "ai-net", "compose_project": "aiplatform"},
        "services": list(rows), "images": {"refs": images, "bytes": sum(r["image_bytes"] for r in rows)},
        "weights_bytes": sum(r["weights_bytes"] for r in rows),
        "bytes": sum(r["image_bytes"] + r["weights_bytes"] for r in rows),
        "budget": {"total_fraction": sum(r["gpu_fraction"] for r in rows), "util_limit": 0.93, "ok": True},
        "problems": list(problems), "warnings": list(warnings),
    }


class FakeBundle:
    """Stands in for `uv run ... tools/bundle.py NAMES --dir KIT ...` (a subprocess.Popen replacement)."""

    def __init__(self, rows=(SILERO,), code: int = 0):
        self.rows, self.code, self.cmd = rows, code, None

    def __call__(self, cmd, **kw):
        self.cmd = cmd
        if self.code:
            out = ["== platform tree", "error: cannot bundle:", "  - silero-vad: image ghcr.io/owner/silero-vad:0.1.0 "
                   "is not present locally (`just build silero-vad` first)"]
        else:
            target = Path(cmd[cmd.index("--dir") + 1])
            (target / "platform" / "services").mkdir(parents=True)
            (target / "platform" / "config.yaml").write_text("gpu_total_gb: 32\nnetwork: ai-net\n")
            for r in self.rows:
                (target / "platform" / "services" / f"{r['name']}.yaml").write_text(f"name: {r['name']}\n")
                w = target / "assets" / r["weights"]["path"]
                w.mkdir(parents=True)
                (w / "model.bin").write_bytes(b"w" * 1000)
            (target / "images").mkdir()
            (target / "images" / "platform.tar").write_bytes(b"tar" * 100)
            (target / "README.md").write_text("bundle.py's own README")
            (target / "bundle.json").write_text(json.dumps({"bundle": "x", **report(*self.rows)}))
            out = ["== platform tree", "  platform/ with ...", "== weights", "  [ 50%] a", "  [100%] weights copied",
                   "== images", "  docker save ...", f"== done: {target}"]
        return types.SimpleNamespace(stdout=io.StringIO("\n".join(out) + "\n"), wait=lambda: self.code)


THIS, PEER = "concord-bench", "concord-label"
SAMPLE_COMPOSE = Path(__file__).resolve().parents[1] / "contracts" / "samples" / "kit" / "compose.yml"


@pytest.fixture
def docker(monkeypatch):
    """Both apps' images present locally (`docker images` lists <app>:0.2.0); `docker save` writes a placeholder."""
    saved = []
    monkeypatch.setattr(export, "image_facts", lambda ref: {"id": f"sha256:{ref}", "bytes": 1_800_000_000})
    monkeypatch.setattr(export, "local_tags", lambda repo: [f"{repo}:0.2.0"])

    def save(ref, dest):
        saved.append(ref)
        dest.write_bytes(b"app image " + ref.encode())

    monkeypatch.setattr(export, "save_image", save)
    return saved


# ---------------------------------------------------------------- kit layout, manifest, verify

def test_full_kit_layout_and_manifest(tmp_path, docker):
    bundle = FakeBundle(rows=(SILERO, WHISPER))
    marks = []
    out = export.export(["silero-vad", "whisper-large-v3"], "full", tmp_path, {THIS: f"{THIS}:0.2.0"},
                        log=lambda m: None, progress=lambda p, m: marks.append(p), runner=bundle)
    k = Path(out["path"])
    assert k.parent == tmp_path and k.name.startswith("concord-offline-silero-vad-whisper-large-v3-")
    cmd = bundle.cmd[bundle.cmd.index("tools/bundle.py") + 1:]
    assert cmd == ["silero-vad", "whisper-large-v3", "--dir", str(k), "--with-images", "--with-weights", "--platform-tree"]
    for rel in ("README.md", "manifest.json", "SHA256SUMS", "install.sh", f"images/{THIS}.tar", "images/platform.tar",
                "platform/config.yaml", "assets/whisper-large-v3/model.bin", "concord/compose.yml", "bundle.json"):
        assert (k / rel).is_file(), rel
    assert not (k / "images" / "concord.tar").exists() and not (k / "images" / f"{PEER}.tar").exists()
    assert docker == [f"{THIS}:0.2.0"]
    m = json.loads((k / "manifest.json").read_text())
    assert m["kind"] == "full" and m["format"] == 1 and m["name"] == k.name
    assert m["concord"] == {"app": THIS, "version": "0.2.0"}
    assert m["apps"] == [{"app": THIS, "image": f"{THIS}:0.2.0", "tar": f"images/{THIS}.tar"}]
    assert m["images"] == {"platform.tar": ["ghcr.io/owner/silero-vad:0.1.0", "vllm/vllm-openai:v0.31.0"],
                           f"{THIS}.tar": [f"{THIS}:0.2.0"]}
    assert [s["name"] for s in m["services"]] == ["silero-vad", "whisper-large-v3"]
    assert m["services"][1]["weights"] == {"kind": "local", "path": "whisper-large-v3", "bytes": 3_090_000_000,
                                           "revision": None}
    assert set(m["files"]) == set(kit.kit_files(k)) and "manifest.json" not in m["files"]
    assert m["bytes"] == sum(v["bytes"] for v in m["files"].values())
    assert marks[-1] == 1.0 and marks == sorted(marks)  # stages in order, never backwards
    assert out["bytes"] == sum(f.stat().st_size for f in k.rglob("*") if f.is_file())
    script = (k / "install.sh").read_text()
    assert os.access(k / "install.sh", os.X_OK) and "@" not in script.split("set -euo pipefail")[1].split("KIT=")[0]
    assert 'KIT_KIND="full"' in script and f'{ENV[THIS]}="{THIS}:0.2.0"' in script
    assert f'{ENV[PEER]}=""' in script and f'THIS_MODULE="{THIS.replace("-", "_")}"' in script
    assert 'GPU_SERVICES="whisper-large-v3"' in script
    readme = (k / "README.md").read_text()
    assert "full kit" in readme and "| whisper-large-v3 | model | vllm |" in readme and "driver >= 580" in readme
    assert f"`images/{THIS}.tar`" in readme and f"http://127.0.0.1:{PORT[THIS]}" in readme
    compose = yaml.safe_load((k / "concord" / "compose.yml").read_text())
    assert compose == one_app_sample(THIS)  # the sample's service for this app, with no peer URL
    assert kit.verify(k) == []


ENV = {"concord-bench": "BENCH_IMAGE", "concord-label": "LABEL_IMAGE"}
PORT = {"concord-bench": 8600, "concord-label": 8620}


def one_app_sample(app: str) -> dict:
    """contracts/samples/kit/compose.yml cut down to one app, as CONTRACTS.md §6 describes a one-app kit."""
    sample = yaml.safe_load(SAMPLE_COMPOSE.read_text())
    svc = sample["services"][app]
    for key in ("BENCH_URL", "LABEL_URL"):
        svc["environment"].pop(key, None)
    svc["volumes"][1] = "${KIT_DIR:?install.sh writes KIT_DIR into concord/.env}/platform:${KIT_DIR}/platform"
    vol = svc["volumes"][-1].split(":")[0]
    return {**sample, "services": {app: svc}, "volumes": {vol: {}}}


def test_two_app_kit_matches_the_contract_sample(tmp_path, docker):
    out = export.export(["silero-vad"], "full", tmp_path, {PEER: None, THIS: None}, log=lambda m: None,
                        runner=FakeBundle())
    k = Path(out["path"])
    assert sorted(docker) == ["concord-bench:0.2.0", "concord-label:0.2.0"]  # the other app's by its contract name
    assert (k / "images" / "concord-bench.tar").read_bytes() == b"app image concord-bench:0.2.0"
    assert (k / "images" / "concord-label.tar").read_bytes() == b"app image concord-label:0.2.0"
    m = json.loads((k / "manifest.json").read_text())
    assert m["apps"] == [{"app": "concord-bench", "image": "concord-bench:0.2.0", "tar": "images/concord-bench.tar"},
                         {"app": "concord-label", "image": "concord-label:0.2.0", "tar": "images/concord-label.tar"}]
    assert m["concord"] == {"app": THIS, "version": "0.2.0"}
    assert m["images"]["concord-bench.tar"] == ["concord-bench:0.2.0"]
    assert m["images"]["concord-label.tar"] == ["concord-label:0.2.0"]
    text = (k / "concord" / "compose.yml").read_text()
    assert yaml.safe_load(text) == yaml.safe_load(SAMPLE_COMPOSE.read_text())
    script = (k / "install.sh").read_text()
    assert 'BENCH_IMAGE="concord-bench:0.2.0"' in script and 'LABEL_IMAGE="concord-label:0.2.0"' in script
    readme = (k / "README.md").read_text()
    assert "Concord Bench and Concord Label" in readme and "http://127.0.0.1:8600" in readme
    assert "http://127.0.0.1:8620" in readme and "`images/concord-label.tar`" in readme
    assert kit.verify(k) == []


def test_kit_with_only_the_other_app(tmp_path, docker, monkeypatch):
    monkeypatch.setattr(export, "local_tags", lambda repo: [f"{repo}:0.1.9", f"{repo}:0.3.0"])
    k = Path(export.export(["silero-vad"], "full", tmp_path, {PEER: f"{PEER}:0.3.0"}, log=lambda m: None,
                           runner=FakeBundle())["path"])
    assert docker == [f"{PEER}:0.3.0"] and not (k / "images" / f"{THIS}.tar").exists()
    m = json.loads((k / "manifest.json").read_text())
    assert m["apps"] == [{"app": PEER, "image": f"{PEER}:0.3.0", "tar": f"images/{PEER}.tar"}]
    assert m["concord"]["app"] == THIS  # this app wrote it, though it is not in it
    assert yaml.safe_load((k / "concord" / "compose.yml").read_text()) == one_app_sample(PEER)
    script = (k / "install.sh").read_text()
    assert f'{ENV[THIS]}=""' in script and f'{ENV[PEER]}="{PEER}:0.3.0"' in script
    # without a tag, the other app's default is its contract name when present, else the first local tag
    assert export.default_image(PEER) == f"{PEER}:0.1.9"
    monkeypatch.setattr(export, "local_tags", lambda repo: [f"{repo}:0.1.9", f"{repo}:0.2.0"])
    assert export.default_image(PEER) == f"{PEER}:0.2.0"


def test_missing_other_app_image_fails_clearly(tmp_path, docker, monkeypatch):
    monkeypatch.setattr(export, "local_tags", lambda repo: [])
    title = "Concord Label" if PEER == "concord-label" else "Concord Bench"
    with pytest.raises(export.ExportError, match=rf"no {title} image \({PEER}:<tag>\) found locally: run `just build` "
                                                 rf"in the {title} repository"):
        export.export(["silero-vad"], "full", tmp_path / "out", {THIS: None, PEER: None}, runner=FakeBundle())
    with pytest.raises(export.ExportError, match="a full kit needs at least one app"):
        export.export(["silero-vad"], "full", tmp_path / "out", {}, runner=FakeBundle())
    with pytest.raises(export.ExportError, match="unknown app concord-improve"):
        export.export(["silero-vad"], "full", tmp_path / "out", {"concord-improve": None}, runner=FakeBundle())
    assert not (tmp_path / "out").exists()


def test_model_kit_has_no_app(tmp_path, docker):
    out = export.export(["whisper-large-v3"], "models", tmp_path, {THIS: None, PEER: None}, log=lambda m: None,
                        runner=FakeBundle(rows=(WHISPER,)))
    k = Path(out["path"])
    assert k.name.startswith("concord-offline-models-whisper-large-v3-")
    assert not list((k / "images").glob("concord*.tar")) and not (k / "concord").exists() and docker == []
    m = json.loads((k / "manifest.json").read_text())
    assert m["kind"] == "models" and m["concord"] == {"app": THIS, "version": "0.2.0"} and m["apps"] == []
    assert list(m["images"]) == ["platform.tar"]
    assert "install.sh --add" in (k / "README.md").read_text()
    # a second export of the same set does not overwrite the first
    again = export.export(["whisper-large-v3"], "models", tmp_path, log=lambda m: None,
                          runner=FakeBundle(rows=(WHISPER,)))
    assert again["path"] == f"{k}-2"


def test_failed_bundle_removes_the_partial_kit(tmp_path, docker):
    with pytest.raises(export.ExportError, match="silero-vad: image .* is not present locally"):
        export.export(["silero-vad"], "full", tmp_path, log=lambda m: None, runner=FakeBundle(code=1))
    assert list(tmp_path.iterdir()) == []


def test_missing_app_image_fails_before_anything_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "image_facts", lambda ref: None)
    with pytest.raises(export.ExportError, match=f"image {THIS}:9.9.9 is not present locally: run `just build`"):
        export.export(["silero-vad"], "full", tmp_path / "out", {THIS: f"{THIS}:9.9.9"}, runner=FakeBundle())
    assert not (tmp_path / "out").exists()


def test_verify_finds_damaged_missing_and_ignores_extra_files(tmp_path, docker):
    k = Path(export.export(["silero-vad"], "full", tmp_path, log=lambda m: None, runner=FakeBundle())["path"])
    (k / "platform" / ".env").write_text("AI_ASSETS=/x\n")  # written by install.sh: not a problem
    assert kit.verify(k) == []
    (k / "images" / "platform.tar").write_bytes(b"TAR" * 100)  # same size, other bytes
    (k / "assets" / "custom-models" / "silero-vad" / "v6.2.3" / "model.bin").unlink()
    (k / "install.sh").write_text("#!/bin/sh\n")
    assert sorted(kit.verify(k)) == ["checksum differs: images/platform.tar",
                                     "missing: assets/custom-models/silero-vad/v6.2.3/model.bin",
                                     f"size differs: install.sh (10 bytes, expected {kit.read_manifest(k)['files']['install.sh']['bytes']})"]
    with pytest.raises(kit.KitError, match="not an offline kit"):
        kit.verify(tmp_path)


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="no sha256sum")
def test_sha256sums_matches_coreutils(tmp_path, docker):
    k = Path(export.export(["silero-vad"], "full", tmp_path, log=lambda m: None, runner=FakeBundle())["path"])
    r = subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS"], cwd=k, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len((k / "SHA256SUMS").read_text().splitlines()) == len(kit.read_manifest(k)["files"]) + 1


def test_cli_verify_kit(tmp_path, docker, capsys):
    from concord_bench import cli

    k = Path(export.export(["silero-vad"], "full", tmp_path, log=lambda m: None, runner=FakeBundle())["path"])
    assert cli.main(["verify-kit", str(k)]) == 0
    (k / "images" / f"{THIS}.tar").write_bytes(b"x")
    assert cli.main(["verify-kit", str(k)]) == 1
    assert f"size differs: images/{THIS}.tar" in capsys.readouterr().out


def test_cli_export_offline_with_both_apps(tmp_path, docker, monkeypatch, capsys):
    from concord_bench import cli

    seen = {}
    monkeypatch.setattr(export, "export", lambda names, kind, dest, apps, name: seen.update(apps=apps) or {"path": "k"})
    assert cli.main(["export-offline", "silero-vad", "--out", str(tmp_path), "--app", THIS,
                     "--app", f"{PEER}={PEER}:0.3.0"]) == 0
    assert seen["apps"] == {THIS: None, PEER: f"{PEER}:0.3.0"}
    assert cli.main(["export-offline", "silero-vad", "--out", str(tmp_path)]) == 0
    assert seen["apps"] is None  # this app only


# ---------------------------------------------------------------- preview

def test_preview_warnings_and_problems(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "dry_run", lambda names: report(
        SILERO, WHISPER, problems=["whisper-large-v3: local model /models/whisper-large-v3 not found at /a"],
        warnings=["together these need 1.2 of the GPU, over util_limit 0.93; ..."]))
    monkeypatch.setattr(export, "image_facts", lambda ref: None)
    monkeypatch.setattr(export, "free_bytes", lambda dest: 10_000_000_000)
    pv = export.preview(["silero-vad", "whisper-large-v3"], "full", tmp_path / "kits", {THIS: f"{THIS}:0.2.0"})
    assert pv["problems"][0].endswith(f"image {THIS}:0.2.0 is not present locally: run `just build` in the "
                                      f"{pv['apps'][0]['title']} repository")
    assert "not found at /a" in pv["problems"][1]
    assert any("over util_limit" in w for w in pv["warnings"])
    assert any("may need up to" in w and "10.0 GB free" in w for w in pv["warnings"])
    assert pv["bytes"] == SILERO["image_bytes"] + WHISPER["image_bytes"] + SILERO["weights_bytes"] + WHISPER["weights_bytes"]
    assert pv["kit"].startswith(str(tmp_path / "kits" / "concord-offline-silero-vad-whisper-large-v3-"))
    rel = export.preview(["silero-vad"], "models", Path("relative/dir"))
    assert rel["problems"][0] == "destination relative/dir is not an absolute path" and rel["apps"] == []
    with pytest.raises(export.ExportError, match="kind must be"):
        export.preview(["silero-vad"], "everything", tmp_path)


def test_preview_counts_the_app_images(tmp_path, monkeypatch, docker):
    monkeypatch.setattr(export, "dry_run", lambda names: report(SILERO))
    monkeypatch.setattr(export, "free_bytes", lambda dest: 10**13)
    one = export.preview(["silero-vad"], "full", tmp_path)
    both = export.preview(["silero-vad"], "full", tmp_path, {THIS: None, PEER: None})
    assert [a["app"] for a in one["apps"]] == [THIS] and one["apps_bytes"] == 1_800_000_000
    assert [a["app"] for a in both["apps"]] == ["concord-bench", "concord-label"] and both["problems"] == []
    assert [a["tar"] for a in both["apps"]] == ["images/concord-bench.tar", "images/concord-label.tar"]
    assert both["bytes"] == one["bytes"] + 1_800_000_000 == report(SILERO)["bytes"] + 3_600_000_000
    none = export.preview(["silero-vad"], "full", tmp_path, {})
    assert none["problems"] == ["a full kit needs at least one app: include Concord Bench, Concord Label or both"]


def test_kit_name():
    from datetime import date

    d = date(2026, 10, 18)
    assert export.kit_name(["silero-vad"], "full", today=d) == "concord-offline-silero-vad-2026-10-18"
    assert export.kit_name(["a", "b", "c", "d"], "models", today=d) == "concord-offline-models-4-models-2026-10-18"
    assert export.kit_name(["a"], "models", "lab", today=d) == "concord-offline-lab-2026-10-18"


# ---------------------------------------------------------------- API + job

def test_api_preview_export_job_and_history(client, tmp_path, monkeypatch, docker):
    from concord_bench.runs import jobs

    monkeypatch.setattr(export, "dry_run", lambda names=None: report(SILERO, WHISPER))
    real = export.run_bundle
    monkeypatch.setattr(export, "run_bundle", lambda args, log, progress, runner=None:
                        real(args, log, progress, runner=FakeBundle(rows=(SILERO,))))
    table = client.get("/api/offline/services").json()
    assert [r["name"] for r in table["services"]] == ["silero-vad", "whisper-large-v3"]
    assert table["services"][0]["model_id"] is None
    apps = {a["app"]: a for a in table["apps"]}
    assert list(apps) == ["concord-bench", "concord-label"] and apps[THIS]["own"] and not apps[PEER]["own"]
    assert apps[THIS]["default_image"] == f"{THIS}:0.2.0" and apps[PEER]["default_image"] == f"{PEER}:0.2.0"
    assert apps[PEER]["images"] == [{"image": f"{PEER}:0.2.0", "present": True, "bytes": 1_800_000_000}]
    assert apps["concord-label"]["port"] == 8620

    dest = tmp_path / "kits"
    pv = client.get("/api/offline/preview", params={"services": "silero-vad", "kind": "full", "dest": str(dest)})
    assert pv.status_code == 200 and pv.json()["problems"] == [] and [a["app"] for a in pv.json()["apps"]] == [THIS]
    pv = client.get("/api/offline/preview", params={"services": "silero-vad", "kind": "full", "dest": str(dest),
                                                     "apps": f"{THIS},{PEER}={PEER}:0.2.0"}).json()
    assert [(a["app"], a["image"]) for a in pv["apps"]] == [("concord-bench", "concord-bench:0.2.0"),
                                                            ("concord-label", "concord-label:0.2.0")]
    assert client.get("/api/offline/preview", params={"services": "silero-vad", "apps": "concord-x"}).status_code == 422
    assert client.post("/api/offline/exports", json={"services": ["silero-vad"], "dest": "relative"}).status_code == 422
    assert client.post("/api/offline/exports", json={"services": ["silero-vad"], "name": "../x"}).status_code == 422
    assert client.post("/api/offline/exports", json={"services": ["silero-vad"], "apps": {}}).status_code == 422

    r = client.post("/api/offline/exports", json={"services": ["silero-vad"], "kind": "full", "dest": str(dest),
                                                  "apps": {THIS: None, PEER: f"{PEER}:0.2.0"}})
    assert r.status_code == 202, r.text
    job_id = r.json()["export"]["job_id"]
    assert r.json()["export"]["status"] == "queued"
    assert r.json()["export"]["concord_image"] == "concord-bench:0.2.0, concord-label:0.2.0"
    assert r.json()["export"]["app_images"] == ["concord-bench:0.2.0", "concord-label:0.2.0"]
    assert jobs.run_pending() == 1
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done", job
    assert job["label"] == "offline full kit · silero-vad"
    assert any("== weights" in line for line in job["log"]) and any("kit written" in line for line in job["log"])
    hist = client.get("/api/offline/exports").json()
    assert hist[0]["status"] == "done" and Path(hist[0]["path"]).parent == dest and hist[0]["bytes"] > 0
    assert hist[0]["job"]["status"] == "done"
    assert (Path(hist[0]["path"]) / "images" / "concord-label.tar").is_file()


def test_failed_export_job_is_recorded_and_retried(client, tmp_path, monkeypatch, docker):
    from concord_bench.runs import jobs

    monkeypatch.setattr(export, "dry_run", lambda names=None: report(SILERO))
    real = export.run_bundle
    fake = {"code": 1}
    monkeypatch.setattr(export, "run_bundle", lambda args, log, progress, runner=None:
                        real(args, log, progress, runner=FakeBundle(code=fake["code"])))
    r = client.post("/api/offline/exports", json={"services": ["silero-vad"], "dest": str(tmp_path / "k")}).json()
    jobs.run_pending()
    rec = client.get("/api/offline/exports").json()[0]
    assert rec["status"] == "failed" and "not present locally" in rec["error"] and rec["path"] is None
    assert client.post(f"/api/jobs/{r['export']['job_id']}/retry").status_code == 200
    assert client.get("/api/offline/exports").json()[0]["status"] == "queued"
    fake["code"] = 0
    jobs.run_pending()
    assert client.get("/api/offline/exports").json()[0]["status"] == "done"


# ---------------------------------------------------------------- install.sh --add merge rules

ENGINES = """# Pinned inference engines.
vllm:
  # checked 2026-10-17
  image: vllm/vllm-openai:v0.31.0
  contract: openai
"""


def _svc(name, port, engine="custom", image=None, alias=None):
    s = {"name": name, "kind": "model", "engine": engine, "port": port, "tasks": ["speech-recognition"]}
    if alias:
        s["alias"] = alias
    if engine == "custom":
        s["build"] = {"context": f"custom/{name}", "image": image or f"{{registry}}/{name}:0.1.0", "base": "py-cpu"}
    else:
        s["model"] = f"/models/{name}"
        s["gpu"] = {"util": 0.1}
    return s


def platform_tree(root: Path, services: list[dict], engines: str = ENGINES, registry: str = "ghcr.io/owner") -> Path:
    (root / "services").mkdir(parents=True)
    (root / "engines").mkdir()
    (root / "custom" / "_bases").mkdir(parents=True)
    (root / "config.yaml").write_text(f"gpu_total_gb: 32\nutil_limit: 0.93\nregistry: {registry}\nnetwork: ai-net\n")
    (root / "engines" / "engines.yaml").write_text(engines)
    (root / "custom" / "_bases" / "bases.yaml").write_text("py-cpu:\n  dockerfile: Dockerfile.py-cpu\n  version: 0.1.1\n")
    for s in services:
        (root / "services" / f"{s['name']}.yaml").write_text(yaml.safe_dump(s, sort_keys=False))
        if s["engine"] == "custom":
            (root / s["build"]["context"]).mkdir(parents=True)
            (root / s["build"]["context"] / "Dockerfile").write_text(f"FROM base\n# {s['build']['image']}\n")
    return root


def fake_kit(root: Path, services: list[dict], **kw) -> Path:
    platform_tree(root / "platform", services, **kw)
    for s in services:
        w = root / "assets" / s["name"]
        w.mkdir(parents=True)
        (w / "model.bin").write_bytes(s["name"].encode() * 10)
    (root / "manifest.json").write_text(json.dumps({"format": 1, "kind": "models", "name": root.name,
                                                    "created_at": "2026-10-18T00:00:00Z", "files": {}}))
    return root


@pytest.fixture
def install(tmp_path):
    root = tmp_path / "install"
    platform_tree(root / "platform", [_svc("silero-vad", 8401, alias="default-vad"),
                                      _svc("whisper-large-v3", 8101, engine="vllm")])
    (root / "assets" / "silero-vad").mkdir(parents=True)
    (root / "concord").mkdir()
    (root / "concord" / ".env").write_text("KIT_DIR=x\nBENCH_IMAGE=concord-bench:0.2.0\nLABEL_IMAGE=concord-label:0.2.0\n")
    return root


def ok(_root):
    return None


def test_merge_adds_a_new_custom_and_engine_service(tmp_path, install):
    k = fake_kit(tmp_path / "kit", [_svc("pyannote-diarization", 8402), _svc("whisper-large-v3-turbo", 8104, engine="vllm")])
    validated = []
    p = merge.add_kit(k, install, log=lambda m: None, validate=validated.append)
    assert p.added == ["pyannote-diarization", "whisper-large-v3-turbo"] and not p.refused
    assert validated == [install / "platform"]
    plat = install / "platform"
    assert (plat / "services" / "pyannote-diarization.yaml").is_file()
    assert (plat / "custom" / "pyannote-diarization" / "Dockerfile").is_file()
    assert (install / "assets" / "whisper-large-v3-turbo" / "model.bin").read_bytes() == b"whisper-large-v3-turbo" * 10
    assert (plat / "engines" / "engines.yaml").read_text() == ENGINES  # same vllm image: untouched
    record = json.loads((install / "kits.json").read_text())
    assert record[0]["name"] == "kit" and record[0]["added"] == p.added


def test_merge_updates_same_image_and_refuses_another_without_replace(tmp_path, install):
    k = fake_kit(tmp_path / "kit", [_svc("silero-vad", 8401, alias="default-vad")])
    assert merge.add_kit(k, install, log=lambda m: None, validate=ok).updated == ["silero-vad"]
    k2 = fake_kit(tmp_path / "kit2", [_svc("silero-vad", 8401, image="{registry}/silero-vad:0.2.0", alias="default-vad")])
    before = (install / "platform" / "services" / "silero-vad.yaml").read_text()
    with pytest.raises(merge.MergeError, match=r"silero-vad: the install has it with image ghcr.io/owner/silero-vad:0.1.0, "
                                               r"the kit with ghcr.io/owner/silero-vad:0.2.0 \(--replace"):
        merge.add_kit(k2, install, log=lambda m: None, validate=ok)
    assert (install / "platform" / "services" / "silero-vad.yaml").read_text() == before
    assert not (install / "assets" / "silero-vad" / "model.bin").read_bytes().startswith(b"x")
    p = merge.add_kit(k2, install, replace=True, log=lambda m: None, validate=ok)
    assert p.replaced == ["silero-vad"]
    assert "0.2.0" in (install / "platform" / "custom" / "silero-vad" / "Dockerfile").read_text()


def test_merge_engine_image_change_needs_replace_and_keeps_comments(tmp_path, install):
    newer = ENGINES.replace("v0.31.0", "v0.32.0")
    k = fake_kit(tmp_path / "kit", [_svc("qwen3-asr-1-7b", 8106, engine="vllm")], engines=newer)
    with pytest.raises(merge.MergeError, match=r"engine vllm: the install runs vllm/vllm-openai:v0.31.0, the kit "
                                               r"vllm/vllm-openai:v0.32.0; --replace moves every vllm service of the "
                                               r"install \(whisper-large-v3\)"):
        merge.add_kit(k, install, log=lambda m: None, validate=ok)
    p = merge.add_kit(k, install, replace=True, log=lambda m: None, validate=ok)
    assert p.engines_replaced == ["vllm"] and p.added == ["qwen3-asr-1-7b"]
    assert (install / "platform" / "engines" / "engines.yaml").read_text() == newer


def test_merge_refuses_port_alias_and_registry_clashes(tmp_path, install):
    k = fake_kit(tmp_path / "kit", [_svc("other-vad", 8401, alias="default-vad")])
    with pytest.raises(merge.MergeError) as exc:
        merge.add_kit(k, install, replace=True, log=lambda m: None, validate=ok)
    assert "other-vad: port 8401 is already used by silero-vad" in str(exc.value)
    assert "other-vad: default-vad is already a name or alias of silero-vad" in str(exc.value)
    k2 = fake_kit(tmp_path / "kit2", [_svc("nemo", 8403)], registry="registry.local/me")
    with pytest.raises(merge.MergeError, match="tagged for registry registry.local/me"):
        merge.add_kit(k2, install, log=lambda m: None, validate=ok)


def test_merge_is_undone_when_the_platform_does_not_validate(tmp_path, install):
    k = fake_kit(tmp_path / "kit", [_svc("silero-vad", 8401, alias="default-vad"), _svc("nemo", 8403)])
    before = {f: f.read_bytes() for f in (install / "platform").rglob("*") if f.is_file()}

    def bad(root):
        raise merge.MergeError("tools/schema.py failed: nemo.yaml: custom_reason is required")

    with pytest.raises(merge.MergeError, match="custom_reason is required"):
        merge.add_kit(k, install, log=lambda m: None, validate=bad)
    after = {f: f.read_bytes() for f in (install / "platform").rglob("*") if f.is_file() and ".offline-backup" not in f.parts}
    assert after == before and not (install / "assets" / "nemo").exists() and not (install / "kits.json").exists()


def test_merge_check_changes_nothing_and_needs_an_install(tmp_path, install):
    k = fake_kit(tmp_path / "kit", [_svc("nemo", 8403)])
    before = sorted(str(f) for f in install.rglob("*"))
    assert merge.add_kit(k, install, check_only=True, log=lambda m: None, validate=ok).added == ["nemo"]
    assert sorted(str(f) for f in install.rglob("*")) == before
    (install / "concord" / ".env").unlink()
    with pytest.raises(merge.MergeError, match="not an offline install"):
        merge.add_kit(k, install, log=lambda m: None, validate=ok)


# ---------------------------------------------------------------- install.sh

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$1 $2" in
  "network inspect") exit 1 ;;
esac
exit 0
"""


def test_install_sh_full_kit_against_a_fake_docker(tmp_path, docker):
    k = Path(export.export(["silero-vad", "whisper-large-v3"], "full", tmp_path / "kits", log=lambda m: None,
                           runner=FakeBundle(rows=(SILERO, WHISPER)))["path"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(stat.S_IRWXU)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "DOCKER_LOG": str(tmp_path / "docker.log"),
           "CONCORD_PROJECT": "concord-test"}
    assert subprocess.run(["bash", "-n", str(k / "install.sh")]).returncode == 0
    r = subprocess.run([str(k / "install.sh"), "--internal"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert calls[:4] == ["info", "compose version", f"load -i {k}/images/{THIS}.tar", f"load -i {k}/images/platform.tar"]
    assert "network create --internal ai-net" in calls
    assert calls[-1] == f"compose --env-file {k}/concord/.env -f {k}/concord/compose.yml up -d"
    assert (k / "platform" / ".env").read_text() == f"AI_ASSETS={k}/assets\nHF_HUB_OFFLINE=1\n"
    assert (k / "concord" / ".env").read_text() == (f"KIT_DIR={k}\n{ENV[THIS]}={THIS}:0.2.0\n"
                                                    "CONCORD_PROJECT=concord-test\nAI_NETWORK=ai-net\n")
    assert (k / "assets" / "hf-cache").is_dir()
    assert f"http://127.0.0.1:{PORT[THIS]}" in r.stdout and f"http://127.0.0.1:{PORT[PEER]}" not in r.stdout
    assert "whisper-large-v3 need an NVIDIA GPU" in r.stderr  # no nvidia-smi on this PATH
    # a second run neither re-verifies nor reloads
    (tmp_path / "docker.log").unlink()
    assert subprocess.run([str(k / "install.sh")], env=env, capture_output=True).returncode == 0
    assert not any(c.startswith("load") for c in (tmp_path / "docker.log").read_text().splitlines())


def fake_docker(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(stat.S_IRWXU)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "DOCKER_LOG": str(tmp_path / "docker.log")}


def test_install_sh_two_app_kit_and_add_runs_in_an_app_image(tmp_path, docker):
    k = Path(export.export(["silero-vad"], "full", tmp_path / "kits", {THIS: None, PEER: None}, log=lambda m: None,
                           runner=FakeBundle())["path"])
    env = fake_docker(tmp_path)
    r = subprocess.run([str(k / "install.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (tmp_path / "docker.log").read_text().splitlines()
    assert [c for c in calls if c.startswith("load")] == [f"load -i {k}/images/concord-bench.tar",
                                                          f"load -i {k}/images/concord-label.tar",
                                                          f"load -i {k}/images/platform.tar"]
    assert "network create ai-net" in calls
    assert (k / "concord" / ".env").read_text() == (f"KIT_DIR={k}\nBENCH_IMAGE=concord-bench:0.2.0\n"
                                                    "LABEL_IMAGE=concord-label:0.2.0\nCONCORD_PROJECT=concord\n"
                                                    "AI_NETWORK=ai-net\n")
    assert "Concord Bench: http://127.0.0.1:8600" in r.stdout and "Concord Label: http://127.0.0.1:8620" in r.stdout

    # a model kit merged in: the merge runs in Concord Bench's image when the install has it, else in Label's
    mk = Path(export.export(["whisper-large-v3"], "models", tmp_path / "kits", log=lambda m: None,
                            runner=FakeBundle(rows=(WHISPER,)))["path"])
    for dotenv, image, module in ((k / "concord" / ".env", "concord-bench:0.2.0", "concord_bench"),
                                  ("KIT_DIR=x\nLABEL_IMAGE=concord-label:0.3.0\n", "concord-label:0.3.0", "concord_label"),
                                  (f"CONCORD_IMAGE={THIS}:0.1.0\n", f"{THIS}:0.1.0", THIS.replace("-", "_"))):
        if isinstance(dotenv, str):
            (k / "concord" / ".env").write_text(dotenv)
        (tmp_path / "docker.log").unlink()
        r = subprocess.run([str(mk / "install.sh"), "--add", str(k)], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        runs = [c for c in (tmp_path / "docker.log").read_text().splitlines() if c.startswith("run ")]
        assert len(runs) == 2 and all(f" {image} python -m {module}.cli add-kit {mk} {k}" in c for c in runs), runs
        assert runs[0].endswith("--check") and f"load -i {mk}/images/platform.tar" in (tmp_path / "docker.log").read_text()


def test_install_sh_refuses_a_damaged_kit_and_the_wrong_mode(tmp_path, docker):
    k = Path(export.export(["silero-vad"], "full", tmp_path / "kits", log=lambda m: None,
                           runner=FakeBundle())["path"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(stat.S_IRWXU)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "DOCKER_LOG": str(tmp_path / "docker.log")}
    (k / "images" / "platform.tar").write_bytes(b"TAR" * 100)
    r = subprocess.run([str(k / "install.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "checksums do not match" in r.stderr
    assert not any(c.startswith("load") for c in (tmp_path / "docker.log").read_text().splitlines())
    r = subprocess.run([str(k / "install.sh"), "--add", str(tmp_path)], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "is not an offline install" in r.stderr
