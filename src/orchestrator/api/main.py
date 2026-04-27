"""FastAPI application factory for the orchestrator API (spec §3, §4).

Use:
  uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from orchestrator.api.middleware import (
    BearerAuthMiddleware,
    BodySizeCapMiddleware,
    CorrelationIdMiddleware,
)
from orchestrator.api.routers.health import router as health_router
from orchestrator.core.settings import get_settings
from orchestrator.db import migrate
from orchestrator.db.pool import (
    PoolError,
    SchemaNotMigratedError,
    SchemaUnknownMigrationError,
    close_pool,
    init_pool,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log = structlog.get_logger()

    # 1. Migrations (sync; offload)
    log.info("api.boot.migrations_starting")
    try:
        await asyncio.to_thread(migrate.run_migrations, settings.database_path)
    except migrate.MigrationError as e:
        log.critical("api.boot.migrations_failed", reason=str(e))
        raise SystemExit(1) from e

    # 2. Pool init
    log.info("api.boot.pool_starting")
    try:
        await init_pool()
    except (SchemaNotMigratedError, SchemaUnknownMigrationError, PoolError) as e:
        log.critical("api.boot.pool_init_failed", reason=str(e))
        raise SystemExit(1) from e

    # 3. Boot metadata
    app.state.boot_time = time.monotonic()
    app.state.git_sha = os.environ.get("GIT_SHA", "unknown")
    log.info("api.boot.complete")

    yield

    log.info("api.shutdown.starting")
    try:
        await close_pool()
    except PoolError as e:
        log.error("api.shutdown.pool_close_failed", reason=str(e))
    log.info("api.shutdown.complete")


def create_app() -> FastAPI:
    """FastAPI application factory.

    Returns a fully-configured FastAPI app with:
      - lifespan that runs migrations + initializes the BL4 pool singleton
      - 4-layer middleware stack (spec §5.1)
      - bearer security_scheme registered for OpenAPI
      - /api/v1/health router mounted
    """
    settings = get_settings()

    app = FastAPI(
        title="lancache_orchestrator API",
        version="0.1.0",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        lifespan=_lifespan,
    )

    # Middleware stack — registered in REVERSE order of how they wrap requests.
    # add_middleware prepends to user_middleware, so the LAST add_middleware
    # call is the OUTERMOST layer at request time.
    # Per spec §5.1 the desired order (outermost → innermost) is:
    #   CorrelationId → BodySizeCap → BearerAuth → CORS
    # So we register in REVERSE: CORS, BearerAuth, BodySizeCap, CorrelationId.

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Correlation-ID"],
    )
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(BodySizeCapMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    # OpenAPI security scheme — middleware does the actual enforcement.
    # This block surfaces the bearer scheme in /api/v1/openapi.json so
    # Swagger UI's Authorize button works.
    _security_schemes: dict[str, Any] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
        }
    }

    _orig_openapi = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = _orig_openapi()
        schema.setdefault("components", {})
        schema["components"]["securitySchemes"] = _security_schemes
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    # Routers
    app.include_router(health_router)

    return app
