"""Settings, read from the environment (and `.env`). One instance, `settings`, used everywhere."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The Concord checkout (fixtures, frontend/dist, .env). In an image the package is installed into
# site-packages, so CONCORD_HOME says where the app's files are.
REPO_ROOT = Path(os.environ.get("CONCORD_HOME") or Path(__file__).resolve().parents[3])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    # Audio, derived files and the database (Q2: local disk only).
    concord_data: Path = Field(default=REPO_ROOT / "data")
    # Phase 3 (Improve) router and UI. Only concord-full sets this.
    feature_improve: bool = False
    # Platform repo: stack.py lives here, and the aicore endpoint map is generated here.
    platform_dir: Path = Field(default=REPO_ROOT.parent.parent / "platform")
    # A ready environment for the platform's tools (containers): stack.py then runs with
    # `uv run --no-sync` in it, so the platform's own .venv on the host is never touched.
    platform_venv: Path | None = None
    # Passed through to aicore; unset means the map bundled with the aicore package.
    aicore_endpoints: Path | None = None
    # localhost on the host; `{name}` inside a container on ai-net.
    ai_host: str = "localhost"
    # Refuse to stop running GPU services when starting what a job needs (jobs wait as `blocked`).
    no_evict: bool = False
    # Files longer than this are flagged at ingest and excluded from runs (Q2).
    max_duration_s: float = 300.0
    # Serve recorded model responses from this folder instead of calling services (tests, demos).
    replay_dir: Path | None = None
    # Write every live model response into this folder (to refresh recordings).
    record_dir: Path | None = None
    # Seconds to wait for `stack.py up` (all services in one call).
    services_timeout_s: int = 20 * 60
    # Built frontend served by FastAPI; absent in development (Vite serves it).
    frontend_dist: Path = Field(default=REPO_ROOT / "frontend" / "dist")

    def export_platform_env(self) -> None:
        """aicore reads AI_HOST and AICORE_ENDPOINTS from the process environment."""
        os.environ["AI_HOST"] = self.ai_host
        if self.aicore_endpoints:
            os.environ["AICORE_ENDPOINTS"] = str(self.aicore_endpoints)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.export_platform_env()
    return s


def reset_settings() -> None:
    """Tests change the environment, then call this."""
    get_settings.cache_clear()
