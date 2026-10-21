"""Talking to the platform about services: health, image tags, and starting what a job needs.

Concord never re-implements compose logic: `ensure_services` runs the platform's own
`tools/stack.py up NAME...` (default eviction, or `--no-evict` when `CONCORD_NO_EVICT`/`no_evict`
is set) and streams its output into the job log. Every required name is passed, not only the
ones that are down, so stack.py never evicts a service this job also needs.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..core.settings import get_settings

Log = Callable[[str], None]


class ServicesBlocked(Exception):
    """The platform could not start a required service (no Docker, missing weights, VRAM, ...)."""


@dataclass
class Health:
    name: str
    state: str  # up | loading | down | unknown | replay
    detail: str = ""

    @property
    def up(self) -> bool:
        return self.state in ("up", "replay")


def replay_mode() -> bool:
    return get_settings().replay_dir is not None


def endpoint(name: str) -> dict[str, Any]:
    import aicore

    return aicore.endpoint(name)


def health(name: str, timeout: float = 2.0) -> Health:
    if replay_mode():
        return Health(name, "replay", "recorded responses")
    try:
        ep = endpoint(name)
    except Exception as exc:  # noqa: BLE001 - unknown service or missing endpoint map
        return Health(name, "unknown", str(exc))
    try:
        r = httpx.get(ep["url"] + ep.get("health_path", "/health"), timeout=timeout)
    except httpx.HTTPError as exc:
        return Health(ep["name"], "down", type(exc).__name__)
    if r.status_code == 200:
        return Health(ep["name"], "up")
    if r.status_code == 503:
        return Health(ep["name"], "loading", r.text[:200])
    return Health(ep["name"], "down", f"HTTP {r.status_code}")


def image_info(name: str) -> dict[str, Any]:
    """Pinned image tag and aiplatform.* labels from the endpoint map, stored on every run."""
    try:
        ep = endpoint(name)
    except Exception:  # noqa: BLE001
        return {}
    return {"image": ep.get("image"), "labels": ep.get("labels", {})}


def gpu_fraction(name: str) -> float:
    try:
        return float(endpoint(name).get("gpu_fraction") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def util_limit() -> float:
    """From the platform's config.yaml; 0.93 if it cannot be read."""
    import yaml

    try:
        cfg = yaml.safe_load((get_settings().platform_dir / "config.yaml").read_text())
        return float(cfg.get("util_limit", 0.93))
    except (OSError, ValueError, AttributeError):
        return 0.93


def platform_env() -> dict[str, str]:
    """The environment for stack.py: the platform's `.env` (as `just` loads it, without overriding
    variables already set) plus ours. Without it AI_ASSETS falls back to the platform default."""
    env = dict(os.environ)
    dotenv = get_settings().platform_dir / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            env.setdefault(key.strip(), value.strip().strip("'\""))
    if get_settings().platform_venv:
        env["UV_PROJECT_ENVIRONMENT"] = str(get_settings().platform_venv)
    return env


def platform_command(tool: str, *args: str, platform_dir: Path | None = None) -> list[str]:
    """`uv run ... python tools/<tool> ARGS` for a platform tool, run with cwd = the platform directory."""
    s = get_settings()
    sync = ["--no-sync"] if s.platform_venv else []
    root = platform_dir or s.platform_dir
    return ["uv", "run", "--quiet", *sync, "--project", str(root), "python", f"tools/{tool}", *args]


def stack_command(names: list[str]) -> list[str]:
    cmd = platform_command("stack.py", "up", *names)
    if get_settings().no_evict:
        cmd.append("--no-evict")
    return cmd


def _wait_ready(names: list[str], log: Log, timeout: float) -> list[str]:
    deadline = time.monotonic() + timeout
    pending = list(names)
    while pending and time.monotonic() < deadline:
        pending = [n for n in pending if not health(n).up]
        if pending:
            time.sleep(2)
    return pending


def ensure_services(names: Iterable[str], log: Log = print, *, runner=subprocess.Popen) -> None:
    """Make sure every named service is healthy, starting them through the platform if needed."""
    names = sorted(set(names))
    if not names or replay_mode():
        return
    states = {n: health(n) for n in names}
    unknown = [n for n, h in states.items() if h.state == "unknown"]
    if unknown:
        raise ServicesBlocked(f"not in the platform endpoint map: {', '.join(unknown)}")
    if all(h.up for h in states.values()):
        return
    loading = [n for n, h in states.items() if h.state == "loading"]
    down = [n for n, h in states.items() if h.state == "down"]
    if loading and not down:
        log(f"waiting for {', '.join(loading)} to finish loading")
        still = _wait_ready(loading, log, get_settings().services_timeout_s)
        if not still:
            return
        down = still
    log(f"starting {', '.join(down)} via the platform (requested set: {', '.join(names)})")
    s = get_settings()
    cmd = stack_command(names)
    log("$ " + " ".join(cmd[cmd.index("tools/stack.py"):]))
    try:
        proc = runner(cmd, cwd=str(s.platform_dir), env=platform_env(), stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError as exc:
        raise ServicesBlocked(f"cannot run the platform tool: {exc}") from None
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if line:
            log(line)
            tail = (tail + [line])[-8:]
    code = proc.wait()
    if code != 0:
        errs = [t for t in tail if t.startswith("error:")] or tail[-3:]
        raise ServicesBlocked("; ".join(e.removeprefix("error: ") for e in errs) or f"stack.py exited {code}")
    still = _wait_ready(names, log, 120)
    if still:
        raise ServicesBlocked(f"started but not healthy: {', '.join(still)}")
    log(f"services ready: {', '.join(names)}")


def fits_together(names: Iterable[str]) -> bool:
    return sum(gpu_fraction(n) for n in set(names)) <= util_limit()
