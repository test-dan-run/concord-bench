"""Writing an offline kit (OFFLINE_EXPORT_PLAN.md §5).

The platform's `tools/bundle.py --dir --with-images --with-weights --platform-tree` writes the models'
images, weights and a trimmed platform tree; it is run the way `ensure_services` runs `stack.py`, with
its output streamed into the job log. Concord then saves its own image (full kits), writes
`concord/compose.yml`, `install.sh` and the README, and checksums every file into `manifest.json`.

Nothing of Concord's state goes into a kit: models are registered again on the offline machine.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..core.models import utcnow
from ..core.settings import get_settings
from ..platform.services import platform_command, platform_env
from . import kit as kitfiles

KINDS = ("full", "models")
CONCORD_VERSION = "0.1.0"
KNOWN_CONCORD_IMAGES = ("concord-lite:0.1.0", "concord-full:0.1.0")

Log = Callable[[str], None]
Progress = Callable[[float, str | None], None]

# Share of the job's progress bar per stage; checksums read every byte again.
STAGES = {"platform tree": (0.0, 0.02), "weights": (0.02, 0.3), "images": (0.3, 0.55), "concord image": (0.55, 0.65),
          "checksums": (0.65, 1.0)}
_PERCENT = re.compile(r"^\s*\[\s*(\d+)%\]")


class ExportError(Exception):
    pass


def concord_images() -> list[str]:
    """Concord images a full kit can carry: the configured one first."""
    return list(dict.fromkeys([get_settings().concord_image, *KNOWN_CONCORD_IMAGES]))


def image_facts(ref: str) -> dict[str, Any] | None:
    """{id, bytes} of a local image, or None when it is missing (or Docker is unavailable)."""
    try:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}} {{.Size}}", ref],
                           capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    image_id, size = r.stdout.split()
    return {"id": image_id, "bytes": int(size)}


def save_image(ref: str, dest: Path) -> None:
    r = subprocess.run(["docker", "save", "-o", str(dest), ref], capture_output=True, text=True)
    if r.returncode != 0:
        raise ExportError(f"docker save {ref} failed: {r.stderr.strip()}")


def free_bytes(dest: Path) -> int | None:
    """Free space where dest is (or would be created)."""
    p = dest
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def kit_name(services: list[str], kind: str, name: str | None = None, today: date | None = None) -> str:
    base = name or ("-".join(services) if len(services) <= 3 else f"{len(services)}-models")
    if kind == "models" and not name:
        base = f"models-{base}"
    return f"concord-offline-{base}-{(today or date.today()).isoformat()}"


def kit_path(dest: Path, name: str) -> Path:
    """dest/name, or dest/name-2, -3 ... when one exists already."""
    p, n = dest / name, 2
    while p.exists():
        p, n = dest / f"{name}-{n}", n + 1
    return p


# ---------------------------------------------------------------- the platform's bundle.py

def dry_run(names: list[str] | None = None) -> dict[str, Any]:
    """bundle.py's report (--dry-run --json) for these services, or for every service."""
    s = get_settings()
    cmd = platform_command("bundle.py", *(names or ["--all"]), "--dry-run", "--json")
    try:
        r = subprocess.run(cmd, cwd=str(s.platform_dir), env=platform_env(), capture_output=True, text=True,
                           timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ExportError(f"cannot run the platform's bundle.py: {exc}") from None
    if r.returncode != 0:
        raise ExportError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                          else f"bundle.py exited {r.returncode}")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise ExportError(f"bundle.py printed no report: {r.stdout[:300]}") from None


def run_bundle(args: list[str], log: Log, progress: Progress, *, runner=subprocess.Popen) -> None:
    """bundle.py with its output in the job log; progress follows its `== stage` and `[ NN%]` lines."""
    s = get_settings()
    cmd = platform_command("bundle.py", *args)
    log("$ " + " ".join(cmd[cmd.index("tools/bundle.py"):]))
    try:
        proc = runner(cmd, cwd=str(s.platform_dir), env=platform_env(), stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError as exc:
        raise ExportError(f"cannot run the platform's bundle.py: {exc}") from None
    stage = "platform tree"
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if not line:
            continue
        log(line)
        tail = (tail + [line])[-12:]
        if line.startswith("== ") and line[3:] in STAGES:
            stage = line[3:]
            progress(STAGES[stage][0], stage)
        elif m := _PERCENT.match(line):
            lo, hi = STAGES[stage]
            progress(lo + (hi - lo) * int(m.group(1)) / 100, None)
    code = proc.wait()
    if code != 0:
        start = next((i for i, t in enumerate(tail) if t.startswith("error:")), max(0, len(tail) - 3))
        raise ExportError("\n".join(tail[start:]).removeprefix("error: ") or f"bundle.py exited {code}")


# ---------------------------------------------------------------- preview and export

def preview(names: list[str], kind: str, dest: Path, concord_image: str | None = None,
            name: str | None = None) -> dict[str, Any]:
    """Sizes, free space and warnings for a kit, before anything is written.

    `problems` would make the export fail (an image or weights missing); `warnings` would not."""
    if kind not in KINDS:
        raise ExportError(f"kind must be one of {', '.join(KINDS)}")
    if not names:
        raise ExportError("choose at least one service")
    rep = dry_run(names)
    problems, warnings = list(rep["problems"]), list(rep["warnings"])
    concord = None
    if kind == "full":
        image = concord_image or get_settings().concord_image
        facts = image_facts(image)
        concord = {"image": image, "present": facts is not None, **(facts or {"id": None, "bytes": None})}
        if facts is None:
            problems.insert(0, f"Concord image {image} is not present locally (`just build-lite` / `just build-full`)")
    if not dest.is_absolute():
        problems.insert(0, f"destination {dest} is not an absolute path")
    total = rep["bytes"] + ((concord or {}).get("bytes") or 0)
    free = free_bytes(dest) if dest.is_absolute() else None
    if free is not None and total > free:
        warnings.append(f"the kit may need up to {total / 1e9:.1f} GB and {dest} has {free / 1e9:.1f} GB free "
                        "(images are counted before shared layers and compression, so it can be smaller)")
    return {
        "kind": kind, "services": rep["services"], "budget": rep["budget"], "platform": rep["platform"],
        "images": rep["images"], "weights_bytes": rep["weights_bytes"], "concord": concord,
        "bytes": total, "free_bytes": free, "dest": str(dest),
        "kit": str(kit_path(dest, kit_name(names, kind, name))) if dest.is_absolute() else None,
        "problems": problems, "warnings": warnings,
    }


def manifest_for(kit: Path, kind: str, bundle: dict[str, Any], concord: dict[str, Any] | None) -> dict[str, Any]:
    keep = ("name", "alias", "kind", "engine", "contract", "api", "tasks", "description", "port", "gpu",
            "gpu_fraction", "image", "image_id", "image_bytes", "weights_bytes")
    services = []
    for r in bundle["services"]:
        w = r.get("weights")
        services.append({**{k: r.get(k) for k in keep},
                         "weights": {k: w[k] for k in ("kind", "path", "bytes", "revision")} if w else None})
    images = {"platform.tar": bundle["images"]["refs"]}
    if concord:
        images["concord.tar"] = [concord["image"]]
    return {
        "format": kitfiles.FORMAT, "kind": kind, "name": kit.name,
        "created_at": utcnow().isoformat(timespec="seconds") + "Z",
        "concord": {"version": CONCORD_VERSION, **({"image": concord["image"], "image_id": concord["id"]}
                                                   if concord else {})},
        "platform": bundle["platform"], "services": services, "images": images,
    }


def export(names: list[str], kind: str, dest: Path, concord_image: str | None = None, name: str | None = None,
           log: Log = print, progress: Progress = lambda p, m: None, *, runner=subprocess.Popen) -> dict[str, Any]:
    """Write a kit into dest. Returns {path, bytes, manifest}. A kit that fails part-way is removed."""
    if kind not in KINDS:
        raise ExportError(f"kind must be one of {', '.join(KINDS)}")
    if not names:
        raise ExportError("choose at least one service")
    if not dest.is_absolute():
        raise ExportError(f"destination {dest} is not an absolute path")
    names = list(dict.fromkeys(names))
    concord = None
    if kind == "full":
        image = concord_image or get_settings().concord_image
        facts = image_facts(image)
        if facts is None:
            raise ExportError(f"Concord image {image} is not present locally (`just build-lite` / `just build-full`)")
        concord = {"image": image, **facts}
    dest.mkdir(parents=True, exist_ok=True)
    kit = kit_path(dest, kit_name(names, kind, name))
    started = datetime.now()
    log(f"writing {kind} kit {kit} with {', '.join(names)}")
    try:
        run_bundle([*names, "--dir", str(kit), "--with-images", "--with-weights", "--platform-tree"], log, progress,
                   runner=runner)
        bundle = json.loads((kit / "bundle.json").read_text())
        if concord:
            progress(STAGES["concord image"][0], "concord image")
            log(f"== concord image\n  docker save {concord['image']}")
            save_image(concord["image"], kit / "images" / "concord.tar")
            (kit / "concord").mkdir()
            (kit / "concord" / "compose.yml").write_text(kitfiles.COMPOSE)
        manifest = manifest_for(kit, kind, bundle, concord)
        gpu = [s["name"] for s in manifest["services"] if s["gpu"]]
        (kit / "install.sh").write_text(kitfiles.install_script(kind, kit.name, concord and concord["image"], gpu))
        (kit / "install.sh").chmod(0o755)
        (kit / "README.md").write_text(kitfiles.readme(manifest))  # replaces bundle.py's platform-only README
        log("== checksums")
        lo, hi = STAGES["checksums"]
        manifest["files"] = kitfiles.checksums(kit, lambda f, m: progress(lo + (hi - lo) * f, m))
        manifest["bytes"] = sum(v["bytes"] for v in manifest["files"].values())
        kitfiles.write_manifest(kit, manifest)
    except BaseException:
        shutil.rmtree(kit, ignore_errors=True)
        log(f"removed the incomplete kit {kit}")
        raise
    size = sum(f.stat().st_size for f in kit.rglob("*") if f.is_file())
    log(f"kit written: {kit} ({size / 1e9:.2f} GB, {len(manifest['files'])} files, "
        f"{(datetime.now() - started).total_seconds():.0f} s)")
    progress(1.0, "done")
    return {"path": str(kit), "bytes": size, "manifest": manifest}
