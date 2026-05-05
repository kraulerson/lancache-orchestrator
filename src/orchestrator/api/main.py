"""FastAPI application factory for the orchestrator API (spec §3, §4).

Use:
  uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import asyncio
import os
import sys
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
from orchestrator.api.routers.platforms import router as platforms_router
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


_LOOPBACK_HOST_VALUES = frozenset({"127.0.0.1", "::1", "localhost"})


def _detect_non_loopback_bind(settings_api_host: str) -> str | None:
    """Return the non-loopback host string if any signal indicates it,
    or None if all known signals say loopback. UAT-3 S2-D — covers
    settings, the UVICORN_HOST env var, and `--host` in argv.
    """
    if settings_api_host not in _LOOPBACK_HOST_VALUES:
        return settings_api_host

    uvicorn_host = os.environ.get("UVICORN_HOST")
    if uvicorn_host and uvicorn_host not in _LOOPBACK_HOST_VALUES:
        return uvicorn_host

    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            value = argv[i + 1]
            if value not in _LOOPBACK_HOST_VALUES:
                return value
        elif arg.startswith("--host="):
            value = arg.split("=", 1)[1]
            if value not in _LOOPBACK_HOST_VALUES:
                return value
    return None


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
        # `from None` breaks the exception cause chain so Starlette's lifespan
        # handler doesn't print the underlying traceback after our structured
        # event line — the structured event IS the operator-facing signal.
        # UAT-3 S2-J full suppression.
        raise SystemExit(1) from None

    # 2. Pool init
    log.info("api.boot.pool_starting")
    try:
        await init_pool()
    except (SchemaNotMigratedError, SchemaUnknownMigrationError, PoolError) as e:
        log.critical("api.boot.pool_init_failed", reason=str(e))
        raise SystemExit(1) from None

    pool_initialized = True

    try:
        # 3. Boot metadata
        app.state.boot_time = time.monotonic()
        app.state.git_sha = os.environ.get("GIT_SHA", "unknown")

        # 4. Deployment-hardening warning: surface non-loopback bind at boot.
        # UAT-3 S2-D: detect non-loopback from any of three signals so an
        # operator running `uvicorn --host 0.0.0.0` from the CLI gets the
        # warning even without ORCH_API_HOST set.
        bind_signal = _detect_non_loopback_bind(settings.api_host)
        if bind_signal is not None:
            log.warning(
                "api.boot.non_loopback_bind_warning",
                api_host=bind_signal,
                hint=(
                    "Binding to a non-loopback interface exposes the API on "
                    "the network. OQ2 loopback enforcement reads scope[client] "
                    "directly — a reverse proxy in front of this app silently "
                    "disables OQ2. Document deployment topology."
                ),
            )

        log.info("api.boot.complete")
        yield
    finally:
        log.info("api.shutdown.starting")
        if pool_initialized:
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

    # Middleware stack — UAT-3 S2-F revised order. CORS is now OUTERMOST so
    # short-circuit responses (401/413) include Access-Control-Allow-Origin
    # headers and the browser surfaces the real status to the operator
    # instead of a misleading "CORS error". CorrelationId moves one layer
    # in; CORS-rejected requests therefore lack a correlation_id in logs,
    # which is the accepted trade — those rejections are rare and almost
    # always client-misconfigured, while the operator-debugging benefit
    # of accurate status visibility for auth/cap rejections is large.
    #
    # add_middleware prepends to user_middleware, so the LAST add_middleware
    # call is the OUTERMOST layer at request time. Order applied (outermost
    # → innermost): CORS → CorrelationId → BodySizeCap → BearerAuth.
    # Registration order (innermost → outermost):

    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(BodySizeCapMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        expose_headers=["X-Correlation-ID"],
    )

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
    app.include_router(platforms_router)

    return app


# Module-level ASGI app for standard `uvicorn module:app` invocations
# (UAT-3 S2-I — operators following stock FastAPI deploy patterns expect
# a module-level `app`). Lazy via PEP 562 __getattr__ so just importing
# the module (e.g. for create_app in tests) doesn't construct settings.
# uvicorn's getattr(module, "app") triggers construction at boot.
_lazy_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    global _lazy_app
    if name == "app":
        if _lazy_app is None:
            _lazy_app = create_app()
        return _lazy_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
