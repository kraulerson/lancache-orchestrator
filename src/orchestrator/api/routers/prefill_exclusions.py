"""Prefill-exclusion overrides (#225) — the operator's control over the
auto-classify-block list.

`GET  /api/v1/prefill-exclusions`                — list all exclusions/allows.
`POST /api/v1/prefill-exclusions/{platform}/{app_id}`  — set mode (allow|exclude),
        source='operator'. A `mode='allow'` row is the STICKY override: the
        auto-classify step never overwrites it, so an un-excluded game keeps
        being cached and is never re-flagged.
`DELETE /api/v1/prefill-exclusions/{platform}/{app_id}` — clear the override.

Read/write only the `prefill_exclusions` table; never touches the cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)

_PLATFORMS = {"steam", "epic"}


class PrefillExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str
    app_id: str
    mode: str
    reason: str | None
    source: str
    updated_at: str


class PrefillExclusionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exclusions: list[PrefillExclusion]
    total: int


class SetExclusionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["allow", "exclude"]
    reason: str | None = None


router = APIRouter(prefix="/api/v1", tags=["prefill-exclusions"])


@router.get(
    "/prefill-exclusions",
    response_model=PrefillExclusionsResponse,
    responses={200: {"description": "All prefill exclusions/allows"}, 503: {"description": "Pool"}},
    summary="List prefill exclusions",
)
async def list_prefill_exclusions(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        rows = await pool.read_all(
            "SELECT platform, app_id, mode, reason, source, updated_at "
            "FROM prefill_exclusions ORDER BY mode, platform, app_id"
        )
    except PoolError as e:
        _log.error("api.prefill_exclusions.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    items = [dict(r) for r in rows]
    body = PrefillExclusionsResponse(
        exclusions=[PrefillExclusion(**r) for r in items], total=len(items)
    )
    return JSONResponse(content=body.model_dump(by_alias=True))


@router.post(
    "/prefill-exclusions/{platform}/{app_id}",
    responses={
        200: {"description": "Override set"},
        400: {"description": "Unknown platform"},
        503: {"description": "Pool"},
    },
    summary="Set a prefill-exclusion override (allow|exclude, operator source)",
)
async def set_prefill_exclusion(
    platform: str,
    app_id: str,
    body: SetExclusionRequest,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    if platform not in _PLATFORMS:
        return JSONResponse(content={"detail": "platform must be steam or epic"}, status_code=400)
    try:
        await pool.execute_write(
            "INSERT INTO prefill_exclusions (platform, app_id, mode, reason, source, updated_at) "
            "VALUES (?, ?, ?, ?, 'operator', CURRENT_TIMESTAMP) "
            "ON CONFLICT(platform, app_id) DO UPDATE SET "
            "  mode=excluded.mode, reason=excluded.reason, source='operator', "
            "  updated_at=CURRENT_TIMESTAMP",
            (platform, app_id, body.mode, body.reason),
        )
    except PoolError as e:
        _log.error("api.prefill_exclusions.write_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    return JSONResponse(content={"platform": platform, "app_id": app_id, "mode": body.mode})


@router.delete(
    "/prefill-exclusions/{platform}/{app_id}",
    responses={200: {"description": "Override cleared (idempotent)"}, 503: {"description": "Pool"}},
    summary="Clear a prefill-exclusion override",
)
async def clear_prefill_exclusion(
    platform: str,
    app_id: str,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        deleted = await pool.execute_write(
            "DELETE FROM prefill_exclusions WHERE platform = ? AND app_id = ?",
            (platform, app_id),
        )
    except PoolError as e:
        _log.error("api.prefill_exclusions.delete_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    return JSONResponse(content={"deleted": deleted})
