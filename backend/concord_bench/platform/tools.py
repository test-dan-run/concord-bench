"""Running the platform's own tools (`tools/bundle.py`, `tools/render.py`) for offline kits.

Concord never re-implements platform logic: the offline export and the model-kit merge run the
platform's scripts with the platform's environment. Models are not started here: the model catalog
does that (`platform.catalog`).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core.settings import get_settings


def platform_env() -> dict[str, str]:
    """The environment for platform tools: the platform's `.env` (as `just` loads it, without overriding
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
