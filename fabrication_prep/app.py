"""The FastAPI application.

* ``/health`` — liveness: the process answers.
* ``/ready`` — readiness: a database round trip at the expected schema revision, the profile catalog
  verified, and the URL-signing keys present. 503 otherwise.
* ``/openapi.json`` — the service's OpenAPI document.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__, db
from .api import router
from .errors import install_handlers
from .logging import configure_logging, describe_db_error
from .profiles import get_catalog
from .settings import get_settings

log = logging.getLogger("fabrication_prep")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    configure_logging(s.log_level)
    s.url_signing_keys()  # fail closed at startup, not at the first download
    get_catalog()  # refuses to start when a shipped profile does not match its digest
    await run_in_threadpool(db.open_pool)
    await run_in_threadpool(db.check_role_posture)
    log.info("fabrication-prep %s started (env=%s)", __version__, s.fabrication_prep_env)
    try:
        yield
    finally:
        await run_in_threadpool(db.close_pool)


def _ready() -> tuple[bool, str]:
    try:
        revision = db.schema_revision()
    except db.DatabaseUnavailable:
        return False, "database pool not open"
    except psycopg.Error as exc:
        log.warning("readiness check failed: %s", describe_db_error(exc))
        return False, "database round-trip failed"
    if revision != db.EXPECTED_SCHEMA_REVISION:
        return False, "schema revision mismatch"
    return True, "ready"


def create_app() -> FastAPI:
    app = FastAPI(
        title="fabrication-prep",
        version=__version__,
        description=(
            "MADFAM fabrication-prep: slices GOC-1 render bundles with the OrcaSlicer CLI using versioned, "
            "digest-pinned printer/filament/process profiles. Outputs (Klipper G-code, Bambu .gcode.3mf) and "
            "slicer-variables.json are served through short-lived signed URLs. Janua service tokens, audience "
            "fabrication-prep-api, scope fabrication-prep:slice."
        ),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    install_handlers(app)
    max_body = get_settings().max_request_bytes

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_body:
            return JSONResponse(
                {
                    "errors": [{"code": "payload_too_large", "message": "request body too large"}],
                    "request_id": request.state.request_id,
                },
                status_code=413,
            )
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        if request.url.path not in ("/health", "/ready"):
            log.info(
                "request",
                extra={
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
        return response

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    def ready() -> JSONResponse:
        ok, reason = _ready()
        return JSONResponse({"status": reason}, status_code=200 if ok else 503)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"service": "fabrication-prep", "version": __version__, "api": "/v1", "openapi": "/openapi.json"}

    app.include_router(router)
    return app


app = create_app()
