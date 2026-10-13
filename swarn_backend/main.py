"""create_app(): routers, middleware, lifespan (bucket, supervisor)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from swarn_backend import __version__
from swarn_backend.api import include_all
from swarn_backend.auth.deps import origin_allowed
from swarn_backend.db.engine import dispose_engine, session_factory
from swarn_backend.jobs.supervisor import Supervisor, set_supervisor
from swarn_backend.settings import get_settings
from swarn_backend.storage.s3 import get_store
from swarn_backend.ws.manager import get_live

log = logging.getLogger("swarn_backend")


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.secret_key == "change-me":
        log.warning("BACKEND_SECRET_KEY is the default — set a real one")
    if not settings.google_enabled:
        log.warning("Google sign-in is not configured%s",
                    " (dev-login enabled)" if settings.dev_login else "")
    store = get_store()
    await store.ensure_bucket()
    supervisor = Supervisor(settings, session_factory(), store, get_live())
    set_supervisor(supervisor)
    await supervisor.start()
    log.info("swarn-backend %s ready on %s", __version__, settings.public_base)
    try:
        yield
    finally:
        await supervisor.stop()
        set_supervisor(None)
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="swarn backend", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.expose_docs else None,
                  redoc_url=None,
                  openapi_url="/openapi.json" if settings.expose_docs else None)

    if settings.cors_origin_list:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list,
                           allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def origin_check(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") \
                and request.url.path.startswith("/api/") \
                and not request.url.path.startswith("/api/auth/cli/") \
                and not origin_allowed(request, settings):
            return JSONResponse({"detail": "request origin not allowed"}, status_code=403)
        return await call_next(request)

    include_all(app)
    return app


app = create_app()
