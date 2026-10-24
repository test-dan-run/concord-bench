"""Settings, read from the environment (and `.env`). One instance, `settings`, used everywhere."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The Concord Bench checkout (fixtures, frontend/dist, .env). In an image the package is installed into
# site-packages, so CONCORD_HOME says where the app's files are.
REPO_ROOT = Path(os.environ.get("CONCORD_HOME") or Path(__file__).resolve().parents[3])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    # Audio, derived files and the database (Q2: local disk only).
    concord_data: Path = Field(default=REPO_ROOT / "data")
    # Platform repo: the aicore endpoint map is generated here.
    platform_dir: Path = Field(default=REPO_ROOT.parent.parent / "platform")
    # Passed through to aicore; unset means the map bundled with the aicore package.
    aicore_endpoints: Path | None = None
    # localhost on the host; `{name}` inside a container on ai-net.
    ai_host: str = "localhost"
    # The catalog service (platform/catalog/README.md): the Model Catalog's model records, leases, output index
    # and the gateway every model call goes through, and the Dataset Catalog. CATALOG_URL, else AICORE_CATALOG.
    catalog_url: str = Field(default="http://127.0.0.1:8700",
                             validation_alias=AliasChoices("CATALOG_URL", "AICORE_CATALOG"))
    # The catalog's UI (models, registration, contract checks), linked from the Models page. Unset: catalog_url.
    catalog_ui_url: str | None = None
    # Where other apps reach this one to read its model outputs (POST <public_url>/api/outputs/batch);
    # registered with the catalog at startup.
    public_url: str = "http://127.0.0.1:8600"
    # Serve recorded model responses from this folder instead of calling services (tests, demos). No
    # catalog is needed then: a stand-in built from the recording answers instead.
    replay_dir: Path | None = None
    # Write every live model response into this folder (to refresh recordings).
    record_dir: Path | None = None
    # Model calls a job keeps in flight at once, by catalog model id, engine or "default"; merged over
    # `runs.executor.CONCURRENCY`. JSON, e.g. INFER_CONCURRENCY='{"meralion-3-3b-asr": 1}'.
    infer_concurrency: dict[str, int] = Field(default_factory=dict)
    # The Dataset Catalog's audio store as this app sees it (DATASET_CATALOG_PLAN.md §4), mounted read-only.
    # Set: ingest uploads originals to the store through the catalog service (at catalog_url) and reads
    # them from here, and datasets can be imported from and published to the catalog. Unset: originals stay
    # in CONCORD_DATA/originals and the Dataset Catalog is not used.
    dataset_store: Path | None = None
    # Concord Label's base URL (SPLIT_PLAN.md §5): "Open in Label" and "Send flags to Label". Unset: no links.
    label_url: str | None = None
    # Built frontend served by FastAPI; absent in development (Vite serves it).
    frontend_dist: Path = Field(default=REPO_ROOT / "frontend" / "dist")

    @field_validator("dataset_store", mode="before")
    @classmethod
    def _empty_is_unset(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v.strip()).expanduser() if v.strip() else None
        return v

    @property
    def catalog_ui(self) -> str:
        return (self.catalog_ui_url or self.catalog_url).rstrip("/")

    @property
    def dataset_catalog_ui(self) -> str:
        """The Dataset Catalog's pages in the catalog UI (DATASET_CATALOG_PLAN.md §6)."""
        return self.catalog_ui + "/datasets"

    def export_platform_env(self) -> None:
        """aicore reads AI_HOST, AICORE_ENDPOINTS and AICORE_CATALOG from the process environment. With
        AICORE_CATALOG set, `aicore.client(name)` calls the catalog's gateway, never a service's own port."""
        os.environ["AI_HOST"] = self.ai_host
        os.environ["AICORE_CATALOG"] = self.catalog_url.rstrip("/")
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
