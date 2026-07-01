"""FastAPI application factory for the orchestrator API (spec §3, §4).

Use:
  uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from orchestrator.api.middleware import (
    BearerAuthMiddleware,
    BodySizeCapMiddleware,
    CorrelationIdMiddleware,
    SourceAllowlistMiddleware,
)
from orchestrator.api.routers.block_list import router as block_list_router
from orchestrator.api.routers.epic_auth import router as epic_auth_router
from orchestrator.api.routers.epic_sync import router as epic_sync_router
from orchestrator.api.routers.fetch_manifests_trigger import (
    router as fetch_manifests_trigger_router,
)
from orchestrator.api.routers.games import router as games_router
from orchestrator.api.routers.health import router as health_router
from orchestrator.api.routers.jobs import router as jobs_router
from orchestrator.api.routers.manifests import router as manifests_router
from orchestrator.api.routers.platforms import router as platforms_router
from orchestrator.api.routers.prefill_trigger import router as prefill_trigger_router
from orchestrator.api.routers.status import router as status_router
from orchestrator.api.routers.sweep_trigger import router as sweep_trigger_router
from orchestrator.api.routers.sync import router as sync_router
from orchestrator.api.routers.validate_trigger import router as validate_trigger_router
from orchestrator.core.logging import configure_logging
from orchestrator.core.net import detect_non_loopback_bind
from orchestrator.core.settings import Settings, get_settings
from orchestrator.db import migrate
from orchestrator.db.pool import (
    PoolError,
    SchemaNotMigratedError,
    SchemaUnknownMigrationError,
    close_pool,
    get_pool,
    init_pool,
)
from orchestrator.jobs.reaper import reap_orphaned_game_status, reap_running_jobs
from orchestrator.jobs.worker import Deps as JobsDeps
from orchestrator.jobs.worker import worker_loop as jobs_worker_loop
from orchestrator.lancache.heartbeat import LancacheProbe
from orchestrator.platform.steam.prefill_driver import SteamPrefillDriver
from orchestrator.scheduler.manager import SchedulerManager
from orchestrator.validator.disk_stat import shutdown_cache_stat_executor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _enforce_lan_bind_policy(settings: Settings) -> None:
    """Fail-closed LAN-bind guard (security priority #1). A non-loopback bind
    MUST declare ORCH_ALLOWED_SOURCE_IPS; otherwise refuse to start. A loopback
    bind is always fine. Called at the top of the lifespan, before migrations,
    so a misconfiguration fails fast."""
    log = structlog.get_logger()
    bind_signal = detect_non_loopback_bind(settings.api_host)
    if bind_signal is None:
        return
    if not settings.allowed_source_ips:
        log.critical(
            "api.boot.lan_bind_without_allowlist",
            api_host=bind_signal,
            hint=(
                "Set ORCH_ALLOWED_SOURCE_IPS to the permitted source(s) before "
                "binding off-loopback. Refusing to start."
            ),
        )
        raise SystemExit(1)
    log.info(
        "api.boot.lan_bind_gated",
        api_host=bind_signal,
        allowed_source_ips=settings.allowed_source_ips,
        note="auth/2fa/schema remain loopback-only (OQ2)",
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log = structlog.get_logger()

    # 0. Fail-closed LAN-bind guard — before any startup work.
    _enforce_lan_bind_policy(settings)

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

    # 2b. Startup job reaper (ID6) — mark all `state='running'` jobs as
    # `failed` before the jobs worker starts polling. The previous worker
    # process died with the previous orchestrator container; those rows
    # are orphaned. Reap before the worker spawns so we don't race a
    # newly-claimed job into the failed bucket.
    try:
        reaped = await reap_running_jobs(get_pool())
        if reaped > 0:
            log.warning("api.boot.reaped_orphan_jobs", count=reaped)
        # Then reset any game left stuck 'downloading' by an interrupted prefill
        # (crash, or a timeout-cancelled handler) — runs after the job reaper so
        # no prefill is in flight (UAT-11 F-INT-1).
        reaped_games = await reap_orphaned_game_status(get_pool())
        if reaped_games > 0:
            log.warning("api.boot.reaped_orphan_downloading_games", count=reaped_games)
    except Exception as e:
        # Defensive: a failed reap shouldn't abort boot — the job rows are
        # still recoverable manually, and the jobs worker won't claim
        # `running` rows anyway (it filters `state='queued'`).
        log.error("api.boot.reaper_failed", reason=str(e)[:200])

    # 3b. Epic client (F6). Pure async-httpx facade over Epic OAuth/library/
    # manifest — no subprocess. Refreshes the access token from the persisted
    # refresh token on demand; raises EpicNotAuthenticated if none is stored.
    from orchestrator.platform.epic.client import EpicClient

    epic_client = EpicClient(settings)
    app.state.epic_client = epic_client
    log.info("api.boot.epic_client_initialized")

    # 3c. Steam prefill driver — drives the host-installed SteamPrefill binary
    # (modern persistent auth) for F5 steam prefill, and reads its auth state
    # for /health. Construction is just path bookkeeping (no IO), so it can't
    # fail boot. Exposed on app.state so /health can call auth_status().
    prefill_driver = SteamPrefillDriver(
        binary=settings.steam_prefill_binary,
        config_dir=settings.steam_prefill_config_dir,
    )
    app.state.prefill_driver = prefill_driver
    log.info(
        "api.boot.steam_prefill_driver_initialized",
        binary=str(settings.steam_prefill_binary),
        config_dir=str(settings.steam_prefill_config_dir),
    )

    # 3d. Data-plane agent control-plane client (re-arch step ②). Constructed
    # unconditionally — construction is pure config (no IO), so it can't fail
    # boot. Handlers and /health only actually call it when settings.agent_enabled
    # is True; otherwise it's inert. Exposed on app.state so /health can probe
    # agent reachability and routed into JobsDeps so the steam/epic/validate
    # seams can delegate to the agent.
    from orchestrator.clients.agent_client import AgentClient

    agent_client = AgentClient(
        base_url=settings.agent_base_url,
        token=settings.orchestrator_token.get_secret_value(),
    )
    app.state.agent_client = agent_client
    log.info(
        "api.boot.agent_client_initialized",
        agent_enabled=settings.agent_enabled,
        agent_base_url=settings.agent_base_url,
    )

    # 4. Jobs worker — spawn the background asyncio task (BL11)
    jobs_shutdown: asyncio.Event = asyncio.Event()
    jobs_deps = JobsDeps(
        pool=get_pool(),
        epic_client=epic_client,
        prefill_driver=prefill_driver,
        agent_client=agent_client,
    )
    jobs_worker_task = asyncio.create_task(
        jobs_worker_loop(
            jobs_deps,
            shutdown=jobs_shutdown,
            poll_interval_sec=settings.jobs_worker_poll_interval_sec,
            job_max_runtime_sec=settings.job_max_runtime_sec,
        ),
        name="jobs_worker",
    )
    app.state.jobs_shutdown = jobs_shutdown
    app.state.jobs_worker_task = jobs_worker_task
    log.info(
        "api.boot.jobs_worker_started",
        poll_interval_sec=settings.jobs_worker_poll_interval_sec,
    )

    # 4b. Scheduler (F12). Registers periodic enqueue callbacks that
    # insert library_sync rows into the jobs table. The BL11 jobs worker
    # (started at step 4) actually executes the handlers — the scheduler
    # only fires cron-style. If start() raises, lifespan catches the
    # error and continues with scheduler_running=False so /health surfaces
    # 503 per JQ3.
    scheduler_manager = SchedulerManager(
        pool=get_pool(),
        enabled=settings.scheduler_enabled,
        library_sync_interval_sec=settings.scheduler_library_sync_interval_sec,
        validation_sweep_enabled=settings.validation_sweep_enabled,
        validation_sweep_cron=settings.validation_sweep_cron,
        scheduled_prefill_enabled=settings.scheduled_prefill_enabled,
        fetch_manifests_enabled=settings.fetch_manifests_enabled,
        fetch_manifests_cron=settings.fetch_manifests_cron,
    )
    try:
        await scheduler_manager.start()
        log.info(
            "api.boot.scheduler_started",
            enabled=settings.scheduler_enabled,
            running=scheduler_manager.running,
            library_sync_interval_sec=settings.scheduler_library_sync_interval_sec,
        )
    except Exception as e:
        log.critical("api.boot.scheduler_start_failed", reason=str(e)[:200])
    app.state.scheduler_manager = scheduler_manager

    # 5. Lancache self-test probe (ID2). Constructed once; the /health
    # router calls `.probe()` per request, which is cached + concurrency-safe.
    app.state.lancache_probe = LancacheProbe(
        url=settings.lancache_heartbeat_url,
        timeout_sec=settings.lancache_probe_timeout_sec,
        cache_ttl_sec=settings.lancache_probe_cache_ttl_sec,
    )
    log.info(
        "api.boot.lancache_probe_initialized",
        url=settings.lancache_heartbeat_url,
        timeout_sec=settings.lancache_probe_timeout_sec,
        cache_ttl_sec=settings.lancache_probe_cache_ttl_sec,
    )

    # 5b. F7 validator self-test. Gates health.validator_healthy: a failed
    # cache-mount check forces /health to 503 until restart.
    from orchestrator.validator.self_test import validator_self_test

    # re-arch ④: pass agent_client so that, when agent_enabled, validator health
    # is sourced from the agent (which owns the cache mount). On the LXC control
    # plane there is no local cache mount, so the local stat would always report
    # unhealthy; the agent is the source of truth.
    app.state.validator_healthy = await validator_self_test(settings, agent_client=agent_client)
    log.info("api.boot.validator_self_test", healthy=app.state.validator_healthy)

    try:
        # 6. Boot metadata
        app.state.boot_time = time.monotonic()
        app.state.git_sha = os.environ.get("GIT_SHA", "unknown")

        log.info("api.boot.complete")
        yield
    finally:
        log.info("api.shutdown.starting")

        # Stop the scheduler FIRST so it doesn't enqueue new work during
        # teardown. scheduler_manager.shutdown() is idempotent + safe
        # when the scheduler never started (e.g., scheduler_enabled=False).
        log.info("api.shutdown.scheduler_stopping")
        try:
            await scheduler_manager.shutdown()
        except Exception as e:
            log.error("api.shutdown.scheduler_stop_failed", reason=str(e)[:200])

        # Stop the jobs worker NEXT so it isn't still holding pool refs when
        # those resources unwind.
        log.info("api.shutdown.jobs_worker_stopping")
        jobs_shutdown.set()
        try:
            await asyncio.wait_for(jobs_worker_task, timeout=5.0)
        except TimeoutError:
            log.warning("api.shutdown.jobs_worker_join_timeout")
            jobs_worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await jobs_worker_task

        if pool_initialized:
            try:
                await close_pool()
            except PoolError as e:
                log.error("api.shutdown.pool_close_failed", reason=str(e))

        # Close the persistent data-plane agent client (re-arch ④ §3b-1).
        # Best-effort: a failed close must not break shutdown.
        with contextlib.suppress(Exception):
            await agent_client.aclose()
        # Tear down the dedicated cache-stat thread pool (#123.4). Idempotent and
        # safe even if validation never ran (the executor is created lazily).
        shutdown_cache_stat_executor()
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

    # LOG-1: install the project's structlog chain (JSON + secret redaction)
    # before the first log line. `python -m uvicorn orchestrator.api.main:app`
    # builds the app via the lazy module `app`, so this is the API process's
    # real startup hook — without it prod ran structlog's default ConsoleRenderer
    # and the redaction processor was never installed.
    configure_logging(log_level=settings.log_level)

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
    # → innermost): CORS → CorrelationId → SourceAllowlist → BodySizeCap →
    # BearerAuth. SourceAllowlist sits just inside CorrelationId so a rejected
    # source still gets a correlation_id, but is gated before body-size/auth.
    # Registration order (innermost → outermost):

    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(BodySizeCapMiddleware)
    app.add_middleware(SourceAllowlistMiddleware)
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
    # Issue #52: operator-facing description for the bearer scheme so
    # Swagger UI's Authorize dialog explains where to get the token and
    # the rotation pointer. Full rotation procedure lives in HANDOFF.md.
    _security_schemes: dict[str, Any] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque",
            "description": (
                "API bearer token. Production: deploy as a Docker secret "
                "mounted at `/run/secrets/orchestrator_token` (32+ ASCII "
                "chars, whitespace stripped). Development: pass via "
                "`ORCH_TOKEN` env var. Sent as `Authorization: Bearer "
                "<token>`. Rotation: see HANDOFF.md (Phase 4). TL;DR — "
                "generate with `openssl rand -hex 32`, update the Docker "
                "secret, restart this service AND the Game_shelf consumer."
            ),
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

    # Convert FastAPI's default 422 Unprocessable Entity → 400 Bad Request
    # for request body and query-parameter validation errors.  This is
    # consistent with the project's established error-surface contract
    # (spec §3 "4xx errors use detail strings", plan BL10 Task 10).
    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Strip `input`/`ctx`/`url` from each error: FastAPI's default payload
        # echoes the rejected `input` (the raw request body), which would reflect
        # a submitted credential (e.g. a Steam password on a malformed auth body)
        # straight back to the client/logs. Keep only type/loc/msg.
        safe = [
            {"type": err.get("type"), "loc": err.get("loc"), "msg": err.get("msg")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content={"detail": safe},
        )

    # Routers
    app.include_router(health_router)
    app.include_router(platforms_router)
    app.include_router(games_router)
    app.include_router(block_list_router)
    app.include_router(jobs_router)
    app.include_router(manifests_router)
    app.include_router(validate_trigger_router)
    app.include_router(sweep_trigger_router)
    app.include_router(fetch_manifests_trigger_router)
    app.include_router(prefill_trigger_router)
    app.include_router(sync_router)
    app.include_router(epic_sync_router)
    app.include_router(epic_auth_router)
    app.include_router(status_router)

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
