from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
import time

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from config import get_settings
from database import build_database
from logging_config import configure_logging
from routers import admin, raters, v1

logger = logging.getLogger(__name__)


async def log_requests(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time

    if request.url.path.startswith(("/api/",)):
        logger.info(
            "HTTP request",
            extra={
                "attributes": {
                    "http.method": request.method,
                    "http.route": request.url.path,
                    "http.status_code": response.status_code,
                    "http.duration_ms": round(duration * 1000, 1),
                }
            },
        )

    return response


async def global_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
        exc_info=True,
        extra={
            "attributes": {
                "http.method": request.method,
                "http.route": request.url.path,
                "exception.type": type(exc).__name__,
            }
        },
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


_COMMIT = os.environ.get("RENDER_GIT_COMMIT", "dev")


async def health():
    return {"status": "healthy", "version": _COMMIT[:8], "commit": _COMMIT}


# Only the versioned programmatic API is meant for outside consumers; the admin
# and rater routes are internal.
_PUBLIC_PATH_PREFIX = "/api/v1"


def _configure_openapi(app: FastAPI, expose_internal_docs: bool) -> None:
    """Scope the generated OpenAPI schema to the public API.

    By default the schema (and therefore /docs, /redoc, /openapi.json) contains
    only the `/api/v1` routes. Filtering the *routes* passed to ``get_openapi``
    rather than the finished ``paths`` dict means the admin/rater request and
    response models are never emitted into ``components`` either, so the docs
    can be shared without leaking the internal surface.
    """

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        routes = app.routes
        if not expose_internal_docs:
            routes = [
                route
                for route in app.routes
                if getattr(route, "path", "").startswith(_PUBLIC_PATH_PREFIX)
            ]
        app.openapi_schema = get_openapi(
            title="Human Rating Platform API",
            version=app.version,
            description=(
                "Programmatic read API for experiment data. Authenticate with a "
                "bearer key: `Authorization: Bearer <key>`."
            ),
            routes=routes,
        )
        return app.openapi_schema

    app.openapi = custom_openapi


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.app.log_level)

    logger.info(
        "Starting Human Rating Platform",
        extra={
            "attributes": {
                "log_level": settings.app.log_level,
                "prolific_enabled": settings.prolific.enabled,
            }
        },
    )

    database = build_database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await database.connect()
        app.state.database = database
        try:
            yield
        finally:
            await database.disconnect()

    app = FastAPI(
        title="Human Rating Platform",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.app.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(log_requests)
    app.add_exception_handler(Exception, global_exception_handler)

    api_router = APIRouter(prefix="/api")
    api_router.include_router(admin.router)
    api_router.include_router(admin.secure_router)
    api_router.include_router(raters.router)
    api_router.include_router(v1.router)
    api_router.add_api_route("/health", health, methods=["GET"])
    app.include_router(api_router)

    _configure_openapi(app, settings.app.expose_internal_docs)

    return app


app = create_app()
