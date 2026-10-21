"""The kit on disk (OFFLINE_EXPORT_PLAN.md §3): manifest, checksums, verification, and the files
Concord writes beside the platform's bundle.

```
concord-offline-<name>-<date>/
  README.md  manifest.json  SHA256SUMS  install.sh
  images/concord.tar        full kit only
  images/platform.tar       every chosen model's image, one docker save
  platform/                 trimmed platform tree (the platform's bundle.py --platform-tree)
  assets/                   weights in AI_ASSETS layout
  concord/compose.yml       full kit only
  bundle.json               the platform's report: services, image ids, weight paths, sizes
```

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

COMPOSE = """\
# Concord on the offline machine. Written by Concord's offline export; install.sh writes .env beside it.
# platform/ and assets/ are mounted at the same absolute path they have on the host: Concord runs
# `docker compose` for the platform against the host's Docker daemon, which resolves the platform's
# ${AI_ASSETS} bind mounts on the host.
name: ${CONCORD_PROJECT:-concord}
services:
  concord:
    image: ${CONCORD_IMAGE:?install.sh writes CONCORD_IMAGE into concord/.env}
    restart: unless-stopped
    networks: [platform]
    ports: ["127.0.0.1:${CONCORD_PORT:-8600}:8600"]
    environment:
      PLATFORM_DIR: ${KIT_DIR}/platform
      AICORE_ENDPOINTS: ${KIT_DIR}/platform/clients/aicore/src/aicore/endpoints.generated.yaml
      UV_OFFLINE: "1"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ${KIT_DIR:?install.sh writes KIT_DIR into concord/.env}/platform:${KIT_DIR}/platform
      - ${KIT_DIR}/assets:${KIT_DIR}/assets:ro
      - data:/data
networks:
  platform:
    name: ${AI_NETWORK:-ai-net}
    external: true
volumes:
  data: {}
"""


def install_script(kind: str, name: str, concord_image: str | None, gpu_services: list[str]) -> str:
    values = {"@KIND@": kind, "@NAME@": name, "@CONCORD_IMAGE@": concord_image or "",
              "@GPU_SERVICES@": " ".join(gpu_services)}
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
    lines = [
        f"# Concord offline kit: `{name}`",
        "",
        ("A **full kit**: Concord, a trimmed platform and the models below. Use it once, to set up a machine "
         "without internet access." if kind == "full" else
         "A **model kit**: the models below only. It adds them to a machine that already has a full kit installed."),
        f"Written {manifest['created_at'][:10]} by Concord {manifest['concord']['version']}"
        + (f" (`{manifest['concord']['image']}`)" if kind == "full" else "") + ".",
        "",
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
            "2. `./install.sh` checks every file against `SHA256SUMS`, loads `images/concord.tar` and "
            "`images/platform.tar`, writes `platform/.env` (`AI_ASSETS=<this folder>/assets`, `HF_HUB_OFFLINE=1`) "
            "and `concord/.env`, creates the platform's Docker network and starts Concord from `concord/compose.yml`. "
            "`--internal` creates the network with no outside access; `CONCORD_PORT` (default 8600) picks the port.",
            "3. Open http://127.0.0.1:8600 and register each model with **Add model**: the compatibility check "
            "runs on this machine's hardware and images. No registrations, scores or datasets come with the kit.",
            "",
            "Concord mounts `platform/` and `assets/` at the same absolute path as on the host, because the "
            "platform's `docker compose` runs on the host's daemon.",
        ]
    else:
        lines += [
            "1. Copy this folder to the offline machine.",
            "2. `./install.sh --add /path/to/install` (the folder a full kit was installed from) checks the "
            "checksums, refuses a service the install already has with a different image unless `--replace` "
            "is given, loads `images/platform.tar`, copies `assets/`, `services/*.yaml` and `custom/<name>/` into "
            "the install and validates its platform.",
            "3. Register the new models in Concord with **Add model**.",
        ]
    lines += [
        "",
        "## Verify",
        "",
        "`sha256sum -c SHA256SUMS` from this folder, or `python -m concord.cli verify-kit <this folder>` "
        "with Concord. `install.sh` does this first.",
        "",
    ]
    return "\n".join(lines)
