"""GET /api/v1/health endpoint per spec §6 + Bible §8.4."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api.dependencies import __version__, get_pool_dep
from orchestrator.core.settings import get_settings

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    version: str
    uptime_sec: int
    scheduler_running: bool
    lancache_reachable: bool
    cache_volume_mounted: bool
    validator_healthy: bool
    git_sha: str


router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        200: {"description": "All subsystems healthy"},
        503: {"description": "At least one subsystem unhealthy", "model": HealthResponse},
    },
)
async def get_health(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic Depends in default
) -> JSONResponse:
    pool_health = await pool.health_check()
    schema_status = await pool.schema_status()

    pool_ok = (
        pool_health["writer"]["healthy"]
        and pool_health["readers"]["healthy"] == pool_health["readers"]["total"]
        and schema_status["current"]
    )

    settings = get_settings()
    cache_path = Path(settings.lancache_nginx_cache_path)
    cache_volume_mounted = cache_path.is_dir()

    # ID2 lancache reachability probe. `app.state.lancache_probe` is built
    # in lifespan startup; `probe()` is cache-fast (no IO most of the time)
    # and concurrency-safe. Tests that use the no-lifespan `unit_app`
    # fixture omit this state — fall back to False rather than crashing.
    probe = getattr(request.app.state, "lancache_probe", None)
    lancache_reachable = False
    if probe is not None:
        lancache_reachable = await probe.probe()

    # F12 scheduler. `app.state.scheduler_manager` is built in lifespan
    # startup; `.running` reads the underlying AsyncIOScheduler. Tests
    # without lifespan omit the state — fall back to False (BL5-stub-like).
    scheduler_manager = getattr(request.app.state, "scheduler_manager", None)
    scheduler_running = False
    if scheduler_manager is not None:
        scheduler_running = scheduler_manager.running

    body = HealthResponse(
        status="ok" if pool_ok else "degraded",
        version=__version__,
        uptime_sec=int(time.monotonic() - request.app.state.boot_time),
        # Remaining BL5 stub — real when validator subsystem ships.
        scheduler_running=scheduler_running,
        lancache_reachable=lancache_reachable,
        cache_volume_mounted=cache_volume_mounted,
        validator_healthy=False,
        # UAT-3 S2-B: /api/v1/health is unauthenticated, so the git_sha
        # is reachable by anyone with network access. Truncate to 8 hex
        # chars — enough to identify a build for ops, not enough for
        # an attacker to fingerprint the exact commit on a public repo.
        # Operators who explicitly want the full SHA should set
        # GIT_SHA="<short>" themselves.
        git_sha=request.app.state.git_sha[:8],
    )

    all_healthy = (
        pool_ok
        and body.scheduler_running
        and body.lancache_reachable
        and body.cache_volume_mounted
        and body.validator_healthy
    )
    return JSONResponse(
        content=body.model_dump(),
        status_code=200 if all_healthy else 503,
    )
