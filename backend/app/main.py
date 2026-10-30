"""FastAPI entry point. Runs as an unprivileged user.

Importing this module touches neither the database nor the file system: the experiment service (SQLite, job manager, worker processes)
is built and started by the lifespan handler, i.e. only when the server really starts."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import errors
from .api.health import router as health_router
from .api.routes.catalog import router as catalog_router
from .api.routes.experiments import router as experiments_router
from .config import get_settings

DESCRIPTION = """
Managed experiments for NetSim Lab. Experiments run as **background jobs in separate worker processes**; request handlers never run them.
All results are **simulator-generated** (or real loopback TCP) and are available **after** a run completes - there is no live metric stream
(see `GET /api/capabilities`, `live_updates`). Errors always use one envelope: `{"error": {"code", "message", "request_id", "issues", "context"}}`.
"""
TAGS = [{"name": "catalog", "description": "approaches, scenarios, limits"}, {"name": "experiments", "description": "start, inspect, cancel"},
        {"name": "results", "description": "metrics, handoffs, events of completed experiments"}, {"name": "predictions", "description": "Stage 6 handoff prediction"},
        {"name": "history", "description": "list/delete past experiments"}, {"name": "export", "description": "JSON / CSV / ZIP"}, {"name": "health"}]


def create_app(service=None) -> FastAPI:
    """`service`: an app.services.experiments.ExperimentService to use instead of the one built from the environment (tests)."""
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.service is None:
            from .services.experiments import create_service
            app.state.service = create_service()
        app.state.service.start()
        try:
            yield
        finally:
            app.state.service.stop()

    app = FastAPI(title=settings.app_name, version=__version__, description=DESCRIPTION, openapi_tags=TAGS, lifespan=lifespan)
    app.state.service = service
    errors.install(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Location", "Retry-After", "Content-Disposition", "Idempotent-Replayed"],
    )
    app.include_router(health_router, prefix="/api")
    app.include_router(catalog_router, prefix="/api")
    app.include_router(experiments_router, prefix="/api")
    return app


app = create_app()
