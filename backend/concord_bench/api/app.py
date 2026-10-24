"""FastAPI app. Serves `/api/*` and, when built, the frontend."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core import db
from ..core.settings import get_settings
from ..platform import catalog
from . import audio, benchmarks, config, datasets, errors, models, offline, outputs, references, runs

log = logging.getLogger("concord_bench")


def create_app(migrate: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if migrate:
            try:
                db.migrate()
            except db.ForeignDatabase as exc:
                log.error("%s", exc)
                raise
        # Where other apps read Bench's outputs; a catalog that is down only delays this to the next start.
        catalog.register_app_in_background()
        yield

    app = FastAPI(title="Concord Bench", version=__version__, lifespan=lifespan)
    for r in (config.router, datasets.router, models.router, runs.router, benchmarks.router, errors.router,
              audio.router, offline.router, references.router, outputs.router):
        app.include_router(r, prefix="/api")

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
