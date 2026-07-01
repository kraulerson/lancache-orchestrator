"""The data-plane agent FastAPI app. Wraps the existing puller / disk-stat /
SteamPrefillDriver and exposes them over HTTP. Runs on the lancache host."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI

from orchestrator.agent.jobs import AgentJobStore
from orchestrator.agent.manifest_archive import manifest_archive_sync_loop
from orchestrator.agent.routers import health, pull, stat, steam
from orchestrator.api.middleware import BearerAuthMiddleware, SourceAllowlistMiddleware
from orchestrator.core.net import detect_non_loopback_bind
from orchestrator.core.settings import Settings, get_settings
from orchestrator.platform.steam.manifest_fetcher import DepotDownloaderManifestFetcher
from orchestrator.platform.steam.prefill_driver import SteamPrefillDriver
from orchestrator.validator.disk_stat import shutdown_cache_stat_executor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# Agent-local auth-exempt set: only the liveness probe bypasses the bearer.
# (The API's AUTH_EXEMPT_PATHS — docs/openapi/status page — do not apply here.)
_AGENT_EXEMPT_PATHS = {("/v1/health", False)}


def _enforce_agent_lan_bind_policy(settings: Settings) -> None:
    """Fail-closed: a non-loopback agent bind MUST declare ORCH_ALLOWED_SOURCE_IPS.

    Mirrors the API's _enforce_lan_bind_policy but reads settings.agent_bind_host.
    """
    log = structlog.get_logger()
    bind_signal = detect_non_loopback_bind(settings.agent_bind_host)
    if bind_signal is None:
        return
    if not settings.allowed_source_ips:
        log.critical(
            "agent.boot.lan_bind_without_allowlist",
            agent_bind_host=bind_signal,
            hint="Set ORCH_ALLOWED_SOURCE_IPS before binding the agent off-loopback.",
        )
        raise SystemExit(1)
    log.info("agent.boot.lan_bind_gated", agent_bind_host=bind_signal)


def create_agent_app(*, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        if not hasattr(app.state, "agent_jobs"):
            app.state.agent_jobs = AgentJobStore()
        if not hasattr(app.state, "agent_bg_tasks"):
            app.state.agent_bg_tasks = set()
        if not hasattr(app.state, "prefill_driver"):
            app.state.prefill_driver = SteamPrefillDriver(
                binary=settings.steam_prefill_binary,
                config_dir=settings.steam_prefill_config_dir,
                # Pin SteamPrefill's HOME so its $HOME/.cache/SteamPrefill manifest
                # cache is steam_prefill_live_cache_dir (the dir the capture reads),
                # by construction — not by relying on the deploy env (UAT-13 F2).
                home=Path(settings.steam_prefill_live_cache_dir).parent.parent,
            )
        if not hasattr(app.state, "manifest_fetcher"):
            app.state.manifest_fetcher = DepotDownloaderManifestFetcher(
                binary=settings.depotdownloader_binary,
                config_dir=settings.depotdownloader_config_dir,
                steam_config_dir=settings.steam_prefill_config_dir,
                archive_dir=settings.steam_manifest_archive_dir,
                delay_sec=settings.manifest_fetch_delay_sec,
                username=settings.steam_username,
            )
        interval = settings.manifest_archive_sync_interval_sec
        if interval > 0:
            sync_task = asyncio.create_task(
                manifest_archive_sync_loop(
                    Path(settings.steam_manifest_cache_dir),
                    Path(settings.steam_manifest_archive_dir),
                    interval,
                )
            )
            app.state.agent_bg_tasks.add(sync_task)
            sync_task.add_done_callback(app.state.agent_bg_tasks.discard)
        try:
            yield
        finally:
            # NEW-1: tear down on shutdown/redeploy — cancel in-flight
            # fire-and-forget tasks (prefill/pull) and shut down the dedicated
            # cache-stat thread pool, so neither is leaked across a restart.
            pending = list(app.state.agent_bg_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            shutdown_cache_stat_executor()

    app = FastAPI(title="lancache-orchestrator data-plane agent", lifespan=_lifespan)
    # Attach eagerly too, so the POST/GET share ONE store instance whether or not
    # the lifespan has run yet (TestClient runs lifespan; the lifespan guards
    # against replacing this instance, so the create/read job_id stays consistent).
    app.state.settings = settings
    app.state.agent_jobs = AgentJobStore()
    app.state.agent_bg_tasks = set()
    app.state.prefill_driver = SteamPrefillDriver(
        binary=settings.steam_prefill_binary,
        config_dir=settings.steam_prefill_config_dir,
        home=Path(settings.steam_prefill_live_cache_dir).parent.parent,
    )
    app.state.manifest_fetcher = DepotDownloaderManifestFetcher(
        binary=settings.depotdownloader_binary,
        config_dir=settings.depotdownloader_config_dir,
        steam_config_dir=settings.steam_prefill_config_dir,
        archive_dir=settings.steam_manifest_archive_dir,
        delay_sec=settings.manifest_fetch_delay_sec,
        username=settings.steam_username,
    )
    app.include_router(health.router)
    app.include_router(pull.router)
    app.include_router(stat.router)
    app.include_router(steam.router)

    # Security wiring (mirrors the API). Middleware added LAST is OUTERMOST, so
    # SourceAllowlist (added second) wraps BearerAuth: a request is first checked
    # against the source-IP allowlist, then the bearer token. The allowlist is a
    # pure no-op when ORCH_ALLOWED_SOURCE_IPS is empty (loopback-only deploy).
    app.add_middleware(BearerAuthMiddleware, exempt_paths=_AGENT_EXEMPT_PATHS)
    app.add_middleware(SourceAllowlistMiddleware)
    return app
