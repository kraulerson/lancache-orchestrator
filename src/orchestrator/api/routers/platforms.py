"""GET /api/v1/platforms — list platform auth/sync status (BL6 / Feature 9)."""

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


_LAST_ERROR_TRUNCATE = 200
_log = structlog.get_logger(__name__)


class PlatformResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["steam", "epic"]
    auth_status: Literal["ok", "expired", "error", "never"]
    auth_method: Literal["steam_cm", "epic_oauth"]
    auth_expires_at: str | None
    last_sync_at: str | None
    last_error: str | None


class PlatformListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platforms: list[PlatformResponse]


router = APIRouter(prefix="/api/v1", tags=["platforms"])


@router.get(
    "/platforms",
    response_model=PlatformListResponse,
    responses={
        200: {"description": "List of all configured platforms"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List all platforms",
    description=(
        "Returns the auth and sync status of every configured platform. "
        "Always returns exactly two rows (steam, epic). Steam is pinned "
        "first in the response order. The `config` field is intentionally "
        "not exposed via this endpoint."
    ),
)
async def list_platforms(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic Depends in default
) -> JSONResponse:
    try:
        rows = await pool.read_all(
            "SELECT name, auth_status, auth_method, auth_expires_at, "
            "last_sync_at, last_error FROM platforms "
            "ORDER BY CASE WHEN name = 'steam' THEN 0 ELSE 1 END, name"
        )
    except PoolError as e:
        _log.error("api.platforms.read_failed", reason=str(e))
        return JSONResponse(
            content={"detail": "database unavailable"},
            status_code=503,
        )

    items = [
        PlatformResponse(
            name=row["name"],
            auth_status=row["auth_status"],
            auth_method=row["auth_method"],
            auth_expires_at=row["auth_expires_at"],
            last_sync_at=row["last_sync_at"],
            last_error=(row["last_error"][:_LAST_ERROR_TRUNCATE] if row["last_error"] else None),
        )
        for row in rows
    ]
    body = PlatformListResponse(platforms=items)
    return JSONResponse(content=body.model_dump())
