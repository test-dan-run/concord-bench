"""`install.sh --add`: a model kit merged into an existing offline install.

Rules, per service of the kit:
- not in the install: added (its manifest, its `custom/<name>/` folder, its weights);
- in the install with the same image: updated in place;
- in the install with a different image: refused, unless `replace`;
- a port or alias another service of the install already uses: always refused;
- its engine missing from the install's engines.yaml: added; there with a different image: refused
  unless `replace`, which moves every service of the install on that engine to the kit's image;
- its custom base missing from the install's bases.yaml: added.

After the merge the install's platform must validate (`tools/schema.py`) and is re-rendered
(`tools/render.py`), so Concord's endpoint map lists the new services. When it does not validate,
manifests, folders, engines.yaml and bases.yaml are put back from `platform/.offline-backup/<time>/`.
Weights are copied file by file; a file already there with the same size is kept. Run as root (the
Concord image, from install.sh), the merge gives what it wrote, and the files Concord's platform
renders as root, back to the owner of the install folder.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ..platform.services import platform_command, platform_env
from .kit import read_manifest

Log = Callable[[str], None]


class MergeError(Exception):
    pass


@dataclass
class Plan:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    engines_added: list[str] = field(default_factory=list)
    engines_replaced: list[str] = field(default_factory=list)
    bases_added: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    @property
    def services(self) -> list[str]:
        return self.added + self.updated + self.replaced


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text()) if path.is_file() else None


@dataclass
class Side:
    """The parts of a platform tree the merge looks at."""

    root: Path
    config: dict[str, Any]
    engines: dict[str, Any]
    bases: dict[str, Any]
    services: dict[str, dict[str, Any]]  # name -> manifest

    @classmethod
    def load(cls, root: Path) -> Side:
        if not (root / "config.yaml").is_file():
            raise MergeError(f"{root} is not a platform tree (no config.yaml)")
        services = {}
        for f in sorted((root / "services").glob("*.y*ml")):
            data = _yaml(f) or {}
            services[data.get("name", f.stem)] = data
        return cls(root, _yaml(root / "config.yaml") or {}, _yaml(root / "engines" / "engines.yaml") or {},
                   _yaml(root / "custom" / "_bases" / "bases.yaml") or {}, services)

    def image(self, s: dict[str, Any]) -> str:
        if s.get("engine") == "custom":
            return s["build"]["image"].replace("{registry}", self.config.get("registry", ""))
        return (self.engines.get(s.get("engine")) or {}).get("image", "?")


def _base(s: dict[str, Any]) -> str:
    return (s.get("build") or {}).get("base") or ("py-cuda" if s.get("gpu") else "py-cpu")


def plan(kit: Path, install: Path, replace: bool = False) -> Plan:
    k, i = Side.load(kit / "platform"), Side.load(install / "platform")
    out = Plan()
    for name, s in k.services.items():
        if s.get("engine") == "custom":
            if k.config.get("registry") != i.config.get("registry"):
                out.refused.append(f"{name}: the kit's images are tagged for registry {k.config.get('registry')}, "
                                   f"the install uses {i.config.get('registry')} (edit registry in one config.yaml)")
            base = _base(s)
            if base != "none" and base not in i.bases and base not in out.bases_added:
                out.bases_added.append(base)
        else:
            e = s.get("engine")
            if e not in i.engines:
                if e not in out.engines_added:
                    out.engines_added.append(e)
            elif i.engines[e].get("image") != k.engines[e].get("image") and e not in out.engines_replaced:
                if replace:
                    out.engines_replaced.append(e)
                else:
                    users = sorted(n for n, o in i.services.items() if o.get("engine") == e)
                    out.refused.append(f"engine {e}: the install runs {i.engines[e].get('image')}, the kit "
                                       f"{k.engines[e].get('image')}; --replace moves every {e} service of the "
                                       f"install ({', '.join(users) or 'none yet'}) to the kit's image")
        if name in i.services:
            old, new = i.image(i.services[name]), k.image(s)
            if old == new:
                out.updated.append(name)
            elif replace:
                out.replaced.append(name)
            else:
                out.refused.append(f"{name}: the install has it with image {old}, the kit with {new} "
                                   "(--replace takes the kit's)")
        else:
            out.added.append(name)
        ids = {s.get("name"), s.get("alias")} - {None}
        for other, o in i.services.items():
            if other == name:
                continue
            if o.get("port") == s.get("port"):
                out.refused.append(f"{name}: port {s.get('port')} is already used by {other} in the install")
            if clash := ids & ({o.get("name"), o.get("alias")} - {None}):
                out.refused.append(f"{name}: {', '.join(sorted(clash))} is already a name or alias of {other}")
    return out


def validate_platform(root: Path) -> None:
    """The install's own `schema.py` then `render.py`, with the platform tools' environment."""
    for tool in ("schema.py", "render.py"):
        r = subprocess.run(platform_command(tool, platform_dir=root), cwd=root, env=platform_env(),
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise MergeError(f"tools/{tool} failed in {root}:\n{(r.stdout + r.stderr).strip()}")


def _copy_assets(src: Path, dest: Path) -> tuple[int, int]:
    """(files copied, bytes copied); files already there with the same size are kept."""
    n = size = 0
    if not src.is_dir():
        return n, size
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        target = dest / f.relative_to(src)
        if target.is_file() and target.stat().st_size == f.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, target)
        n += 1
        size += f.stat().st_size
    return n, size


def _set_engine_image(text: str, old: str, new: str) -> str | None:
    """engines.yaml with one engine's image changed in place (comments kept), or None if ambiguous."""
    line = f"image: {old}"
    return text.replace(line, f"image: {new}") if text.count(line) == 1 else None


def match_owner(install: Path) -> None:
    """chown platform/, assets/ and kits.json to the install folder's owner (only when running as root)."""
    if os.geteuid() != 0:
        return
    st = install.stat()
    for top in (install / "platform", install / "assets", install / "kits.json"):
        paths = [top, *top.rglob("*")] if top.is_dir() else [top] if top.exists() else []
        for f in paths:
            fs = f.lstat()
            if (fs.st_uid, fs.st_gid) != (st.st_uid, st.st_gid):
                os.chown(f, st.st_uid, st.st_gid, follow_symlinks=False)


def add_kit(kit: Path, install: Path, replace: bool = False, check_only: bool = False, log: Log = print,
            validate: Callable[[Path], None] = validate_platform) -> Plan:
    kit, install = kit.resolve(), install.resolve()
    manifest = read_manifest(kit)
    if not (install / "concord" / ".env").is_file():
        raise MergeError(f"{install} is not an offline install (no concord/.env); install a full kit there first")
    p = plan(kit, install, replace)
    if p.refused:
        raise MergeError("refused:\n  - " + "\n  - ".join(p.refused))
    for what, names in (("add", p.added), ("update", p.updated), ("replace", p.replaced),
                        ("add engine", p.engines_added), ("replace engine", p.engines_replaced),
                        ("add base", p.bases_added)):
        if names:
            log(f"{what}: {', '.join(names)}")
    if check_only:
        return p

    k, i = Side.load(kit / "platform"), Side.load(install / "platform")
    (i.root / ".offline-backup").mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=i.root / ".offline-backup"))
    touched: list[tuple[str, bool]] = []  # (path relative to the platform, existed before)

    def save(rel: str) -> None:
        src = i.root / rel
        if src.exists():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            (shutil.copytree if src.is_dir() else shutil.copyfile)(src, backup / rel)
        touched.append((rel, src.exists()))

    def put(rel: str) -> None:
        save(rel)
        src, dest = k.root / rel, i.root / rel
        if dest.is_dir():
            shutil.rmtree(dest)
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)

    try:
        for name in p.services:
            s = k.services[name]
            put(f"services/{name}.yaml")
            if s.get("engine") == "custom":
                put(s["build"]["context"])
        if p.engines_added or p.engines_replaced:
            rel = "engines/engines.yaml"
            save(rel)
            text = (i.root / rel).read_text()
            for e in p.engines_replaced:
                changed = _set_engine_image(text, i.engines[e]["image"], k.engines[e]["image"])
                if changed is None:
                    i.engines[e] = k.engines[e]
                    text = yaml.safe_dump(i.engines, sort_keys=False)
                else:
                    text = changed
            for e in p.engines_added:
                text = text.rstrip("\n") + "\n\n" + yaml.safe_dump({e: k.engines[e]}, sort_keys=False)
            (i.root / rel).write_text(text)
        if p.bases_added:
            rel = "custom/_bases/bases.yaml"
            save(rel)
            text = (i.root / rel).read_text().rstrip("\n") + "\n"
            for b in p.bases_added:
                text += yaml.safe_dump({b: k.bases[b]}, sort_keys=False)
                dockerfile = f"custom/_bases/{k.bases[b]['dockerfile']}"
                if not (i.root / dockerfile).exists():
                    put(dockerfile)
            (i.root / rel).write_text(text)
        log("validating the install's platform")
        validate(i.root)
    except Exception:
        for rel, existed in reversed(touched):
            dest = i.root / rel
            if dest.is_dir():
                shutil.rmtree(dest)
            elif dest.exists():
                dest.unlink()
            if existed:
                (shutil.copytree if (backup / rel).is_dir() else shutil.copyfile)(backup / rel, dest)
        log(f"merge undone; the install's platform is as it was (backup in {backup})")
        raise

    n, size = _copy_assets(kit / "assets", install / "assets")
    log(f"weights: {n} file(s) copied ({size / 1e6:.1f} MB)")
    record = install / "kits.json"
    kits = json.loads(record.read_text()) if record.is_file() else []
    kits.append({"name": manifest.get("name"), "kind": manifest.get("kind"), "created_at": manifest.get("created_at"),
                 "merged_at": datetime.now().isoformat(timespec="seconds"), **asdict(p)})
    record.write_text(json.dumps(kits, indent=1) + "\n")
    match_owner(install)
    log(f"merged {manifest.get('name')} into {install}: {', '.join(p.services)}")
    return p
