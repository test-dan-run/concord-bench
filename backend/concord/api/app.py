"""FastAPI app. Serves `/api/*` and, when built, the frontend.

The Improve router (Phase 3) is mounted only when `FEATURE_IMPROVE` is on *and* `concord_improve`
imports; `GET /api/config` reports which.
"""

from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..core import db
from ..core.settings import get_settings
from . import audio, benchmarks, config, datasets, errors, label, models, offline, runs, sync

log = logging.getLogger("concord")


def improve_router():
    """The Improve router, or None when the flag is off or the package is not installed (lite image)."""
    if not get_settings().feature_improve:
        return None
    try:
        mod = importlib.import_module("concord_improve")
    except ImportError:
        log.warning("FEATURE_IMPROVE is set but concord_improve is not installed; Improve stays off")
        return None
    return getattr(mod, "router", None)


def create_app(migrate: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if migrate:
            db.migrate()
        yield

    app = FastAPI(title="Concord", version="0.1.0", lifespan=lifespan)
    improve = improve_router()
    app.state.features = {"improve": improve is not None}
    for r in (config.router, datasets.router, models.router, runs.router, benchmarks.router, errors.router,
              audio.router, label.router, sync.router, offline.router):
        app.include_router(r, prefix="/api")
    if improve is not None:
        app.include_router(improve, prefix="/api/improve")

    dist = settings.frontend_dist
    if (dist / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path == "api" or path.startswith("api/"):
                raise HTTPException(404, "Not Found")  # unknown API routes never fall through to the SPA
            f = (dist / path).resolve()
            if path and f.is_file() and f.is_relative_to(dist.resolve()):
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
