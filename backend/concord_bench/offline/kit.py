"""The kit on disk: manifest, checksums, verification, and the files
Concord writes beside the platform's bundle.

```
concord-offline-<name>-<date>/
  README.md  manifest.json  SHA256SUMS  install.sh
  images/concord-bench.tar  full kit with Concord Bench
  images/concord-label.tar  full kit with Concord Label
  images/platform.tar       every chosen model's image, one docker save
  platform/                 trimmed platform tree (the platform's bundle.py --platform-tree)
  assets/                   weights in AI_ASSETS layout
  concord/compose.yml       full kit only: one service per app in the kit (CONTRACTS.md §6)
  bundle.json               the platform's report: services, image ids, weight paths, sizes
```

A full kit holds one or both apps; `manifest.json` lists them as `"apps": [{"app", "image", "tar"}]`.

`manifest.json` lists the sha256 of every other file; `SHA256SUMS` holds the same checksums (plus
the manifest's) in `sha256sum -c` format, so `install.sh` can verify a kit before Concord is loaded.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"
SUMS = "SHA256SUMS"
FORMAT = 1
CHUNK = 8 << 20
INSTALL_SH = Path(__file__).with_name("install.sh")

Progress = Callable[[float, str | None], None]


class KitError(Exception):
    pass


def kit_files(kit: Path) -> list[str]:
    """Every file of the kit (relative, sorted) except the two checksum files."""
    out = []
    for dirpath, dirnames, filenames in os.walk(kit):
        dirnames.sort()
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(kit).as_posix()
            if rel not in (MANIFEST, SUMS):
                out.append(rel)
    return sorted(out)


def sha256_file(path: Path, on_bytes: Callable[[int], None] | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(CHUNK):
            h.update(block)
            if on_bytes:
                on_bytes(len(block))
    return h.hexdigest()


def checksums(kit: Path, progress: Progress | None = None) -> dict[str, dict[str, Any]]:
    """{relative path: {sha256, bytes}} for every kit file, with progress by bytes read."""
    files = kit_files(kit)
    total = sum((kit / f).stat().st_size for f in files) or 1
    done = 0

    def tick(n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(done / total, None)

    out = {}
    for rel in files:
        if progress:
            progress(done / total, f"checksum {rel}")
        out[rel] = {"sha256": sha256_file(kit / rel, tick), "bytes": (kit / rel).stat().st_size}
    return out


def write_manifest(kit: Path, manifest: dict[str, Any]) -> None:
    """manifest.json, then SHA256SUMS covering the manifest and every file it lists."""
    (kit / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")
    lines = [f"{v['sha256']}  {rel}" for rel, v in manifest["files"].items()]
    lines.append(f"{sha256_file(kit / MANIFEST)}  {MANIFEST}")
    (kit / SUMS).write_text("\n".join(lines) + "\n")


def read_manifest(kit: Path) -> dict[str, Any]:
    try:
        m = json.loads((kit / MANIFEST).read_text())
    except FileNotFoundError:
        raise KitError(f"{kit} has no {MANIFEST}: not an offline kit") from None
    except ValueError as exc:
        raise KitError(f"{kit / MANIFEST} is not valid JSON: {exc}") from None
    if m.get("format") != FORMAT:
        raise KitError(f"{kit / MANIFEST} has format {m.get('format')!r}; this Concord reads format {FORMAT}")
    return m


def verify(kit: Path, progress: Progress | None = None) -> list[str]:
    """Problems with the kit's files against manifest.json (empty when the kit is intact).

    Files that are not in the manifest are ignored: installing writes `.env` files into the kit."""
    m = read_manifest(kit)
    files: dict[str, dict[str, Any]] = m.get("files") or {}
    total = sum(v["bytes"] for v in files.values()) or 1
    done = 0
    problems = []
    for rel, want in files.items():
        f = kit / rel
        if not f.is_file():
            problems.append(f"missing: {rel}")
        elif f.stat().st_size != want["bytes"]:
            problems.append(f"size differs: {rel} ({f.stat().st_size} bytes, expected {want['bytes']})")
        elif sha256_file(f) != want["sha256"]:
            problems.append(f"checksum differs: {rel}")
        done += want["bytes"]
        if progress:
            progress(done / total, rel)
    return problems


# ---------------------------------------------------------------- files Concord writes into a kit

# How each app runs in a kit (CONTRACTS.md §6), so either app can write the compose entry for the other.
# `url_env` is the variable the other app gets, pointing at this one, when both are in the kit.
APPS: dict[str, dict[str, Any]] = {
    "concord-bench": {"title": "Concord Bench", "port": 8600, "image_env": "BENCH_IMAGE", "volume": "bench-data",
                      "url_env": "BENCH_URL", "module": "concord_bench"},
    "concord-label": {"title": "Concord Label", "port": 8620, "image_env": "LABEL_IMAGE", "volume": "label-data",
                      "url_env": "LABEL_URL", "module": "concord_label"},
}


def app_tar(app: str) -> str:
    """An app's image tar, relative to the kit."""
    return f"images/{app}.tar"


def app_url(app: str, host: str | None = None) -> str:
    return f"http://{host or app}:{APPS[app]['port']}"


COMPOSE_HEAD = """\
# Concord on the offline machine (CONTRACTS.md §6). Written by Concord's offline export; install.sh writes
# KIT_DIR and the image tags into concord/.env beside it. platform/ and assets/ are mounted at the same
# absolute path they have on the host: the apps run `docker compose` for the platform against the host's
# Docker daemon, which resolves the platform's ${AI_ASSETS} bind mounts on the host.
name: ${CONCORD_PROJECT:-concord}
services:
"""

COMPOSE_SERVICE = """\
  {app}:
    image: ${{{image_env}:?install.sh writes {image_env} into concord/.env}}
    hostname: {app}
    restart: unless-stopped
    networks: [platform]
    ports: ["127.0.0.1:{port}:{port}"]
    environment:
      PLATFORM_DIR: ${{KIT_DIR}}/platform
      AICORE_ENDPOINTS: ${{KIT_DIR}}/platform/clients/aicore/src/aicore/endpoints.generated.yaml
      UV_OFFLINE: "1"
{peers}    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - {platform}:${{KIT_DIR}}/platform
      - ${{KIT_DIR}}/assets:${{KIT_DIR}}/assets:ro
      - {volume}:/data
"""

COMPOSE_TAIL = """\
networks:
  platform:
    name: ${AI_NETWORK:-ai-net}
    external: true
volumes:
"""


def compose(apps: list[str]) -> str:
    """concord/compose.yml for these apps: one service each, and the peer URLs only when both are in the kit."""
    apps = [a for a in APPS if a in apps]
    if not apps:
        raise KitError("a full kit needs at least one app")
    text = COMPOSE_HEAD
    for i, app in enumerate(apps):
        a = APPS[app]
        peers = "".join(f"      {APPS[o]['url_env']}: {app_url(o)}\n" for o in apps if o != app)
        platform = "${KIT_DIR:?install.sh writes KIT_DIR into concord/.env}/platform" if i == 0 else "${KIT_DIR}/platform"
        text += COMPOSE_SERVICE.format(app=app, image_env=a["image_env"], port=a["port"], peers=peers,
                                       platform=platform, volume=a["volume"])
    return text + COMPOSE_TAIL + "".join(f"  {APPS[a]['volume']}: {{}}\n" for a in apps)


def install_script(kind: str, name: str, apps: dict[str, str], gpu_services: list[str], this_app: str) -> str:
    """install.sh from its template. `apps` maps each app in the kit to its image (empty for a model kit);
    `this_app` wrote the kit: its CLI merges a model kit into an install whose concord/.env predates S7."""
    values = {"@KIND@": kind, "@NAME@": name, "@BENCH_IMAGE@": apps.get("concord-bench", ""),
              "@LABEL_IMAGE@": apps.get("concord-label", ""), "@GPU_SERVICES@": " ".join(gpu_services),
              "@THIS_MODULE@": APPS[this_app]["module"]}
    text = INSTALL_SH.read_text()
    for k, v in values.items():
        text = text.replace(k, v)
    return text


def _gb(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def _service_row(s: dict[str, Any]) -> str:
    share = f"{s['gpu_fraction']:.3f}" if s["gpu"] else "CPU"
    return (f"| {s['name']} | {s['kind']} | {s['engine']} | {', '.join(s['tasks'])} | `{s['image']}` | "
            f"{_gb(s['image_bytes'])} | {_gb(s['weights_bytes'])} | {share} |")


def readme(manifest: dict[str, Any]) -> str:
    kind, name = manifest["kind"], manifest["name"]
    plat = manifest["platform"]
    services = manifest["services"]
    gpu = [s for s in services if s["gpu"]]
    total = sum(s["gpu_fraction"] for s in services)
    apps = manifest.get("apps") or []
    writer = APPS[manifest["concord"]["app"]]
    titles = " and ".join(APPS[a["app"]]["title"] for a in apps)
    lines = [
        f"# Concord offline kit: `{name}`",
        "",
        (f"A **full kit**: {titles}, a trimmed platform and the models below. Use it once, to set up a machine "
         "without internet access." if kind == "full" else
         "A **model kit**: the models below only. It adds them to a machine that already has a full kit installed."),
        f"Written {manifest['created_at'][:10]} by {writer['title']} {manifest['concord']['version']}.",
        "",
    ]
    if apps:
        lines += [
            "| app | image | tar | address after install |",
            "|---|---|---|---|",
            *[f"| {APPS[a['app']]['title']} | `{a['image']}` | `{a['tar']}` | http://127.0.0.1:{APPS[a['app']]['port']} |"
              for a in apps],
            "",
        ]
        if len(apps) > 1:
            lines += ["Both apps run from one `concord/compose.yml` and know each other's address (`BENCH_URL`, "
                      "`LABEL_URL`), so Send to Bench and Open in Label work between them.", ""]
    lines += [
        "| service | kind | engine | tasks | image | image size | weights | GPU |",
        "|---|---|---|---|---|---|---|---|",
        *[_service_row(s) for s in services],
        "",
        (f"VRAM: {total:.3f} of the {plat['gpu_total_gb']:g} GB GPU ({total * plat['gpu_total_gb']:.1f} GB) for all of "
         f"them together, util_limit {plat['util_limit']}. "
         + ("They fit on the GPU at once." if total <= plat["util_limit"] else
            "They do not all fit at once; Concord starts the subset each job needs.")
         if gpu else "Every service runs on the CPU."),
        "",
        "## Requirements",
        "",
        "- Linux with Docker and the compose plugin, and `sha256sum` (coreutils).",
    ]
    if gpu:
        lines += [
            f"- An NVIDIA GPU with the container toolkit and driver >= 580 (the CUDA 13 images), for "
            f"{', '.join(s['name'] for s in gpu)}.",
            f"- `gpu.util` and `util_limit` are fractions of `gpu_total_gb: {plat['gpu_total_gb']:g}` in "
            "`platform/config.yaml`. On a different GPU, edit it there after `./install.sh` (the checksums cover the "
            "file as exported); `install.sh` warns when `nvidia-smi` disagrees.",
        ]
    lines += ["", "## Install", ""]
    if kind == "full":
        lines += [
            "1. Copy this folder to the offline machine, to where it should live: it becomes the install, and "
            "its path is written into `platform/.env` and `concord/.env` (to move it later, run `./install.sh` again).",
            "2. `./install.sh` checks every file against `SHA256SUMS`, loads "
            + ", ".join(f"`{a['tar']}`" for a in apps) + " and `images/platform.tar`, writes `platform/.env` "
            "(`AI_ASSETS=<this folder>/assets`, `HF_HUB_OFFLINE=1`) and `concord/.env`, creates the platform's Docker "
            f"network and starts {titles} from `concord/compose.yml`. `--internal` creates the network with no "
            "outside access.",
            "3. Register each model in the platform's model catalog, which verifies it on this machine's hardware "
            "and images, then open " + " and ".join(f"http://127.0.0.1:{APPS[a['app']]['port']}" for a in apps)
            + ". No registrations, scores or datasets come with the kit.",
            "",
            "The apps mount `platform/` and `assets/` at the same absolute path as on the host, because the "
            "platform's `docker compose` runs on the host's daemon.",
        ]
    else:
        lines += [
            "1. Copy this folder to the offline machine.",
            "2. `./install.sh --add /path/to/install` (the folder a full kit was installed from) checks the "
            "checksums, refuses a service the install already has with a different image unless `--replace` "
            "is given, loads `images/platform.tar`, copies `assets/`, `services/*.yaml` and `custom/<name>/` into "
            "the install and validates its platform.",
            "3. Register the new models in the install's model catalog.",
        ]
    lines += [
        "",
        "## Verify",
        "",
        f"`sha256sum -c SHA256SUMS` from this folder, or `python -m {writer['module']}.cli verify-kit <this folder>` "
        f"with {writer['title']}. `install.sh` does this first.",
        "",
    ]
    return "\n".join(lines)
