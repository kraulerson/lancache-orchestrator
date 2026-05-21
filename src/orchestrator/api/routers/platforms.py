"""GET /api/v1/platforms — list platform auth/sync status (BL6 / Feature 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

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


class PlatformsMeta(BaseModel):
    """UAT-5 U5-6: envelope-meta parity with games/jobs/manifests.

    Platforms is a fixed 2-row table so pagination doesn't apply; we still
    emit a meta object for envelope-shape consistency. `applied_filters` /
    `applied_sort` are always empty (no filtering/sort surface configured
    on this endpoint), and `total` is the row count.
    """

    model_config = ConfigDict(extra="forbid")
    total: int
    applied_filters: dict[str, dict[str, Any]] = {}
    applied_sort: list[Any] = []


class PlatformListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platforms: list[PlatformResponse]
    meta: PlatformsMeta


router = APIRouter(prefix="/api/v1", tags=["platforms"])


@router.get(
    "/platforms",
    response_model=PlatformListResponse,
    responses={
        200: {"description": "List of all configured platforms"},
        400: {"description": "Unknown query parameter"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List all platforms",
    description=(
        "Returns the auth and sync status of every configured platform. "
        "Always returns exactly two rows (steam, epic). Steam is pinned "
        "first in the response order. The `config` field is intentionally "
        "not exposed via this endpoint. Filtering/sort/pagination are not "
        "supported (the result set is bounded at 2); unknown query "
        "parameters are rejected with 400 for convention parity with the "
        "other F9 read endpoints."
    ),
)
async def list_platforms(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic Depends in default
) -> JSONResponse:
    # UAT-5 U5-5: reject ANY query parameter. Platforms doesn't support
    # filter/sort/pagination/include; the other 3 F9 endpoints all 400 on
    # unknown params, this endpoint must too for cross-router consistency.
    if request.query_params:
        unknown = sorted(request.query_params.keys())
        return JSONResponse(
            content={"detail": f"unknown query parameter(s): {unknown}"},
            status_code=400,
        )

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

    # UAT-5 U5-2 parity: defensive per-row construction. Out-of-Literal
    # values in `name`/`auth_status`/`auth_method` would otherwise raise
    # ValidationError → 500.
    items: list[PlatformResponse] = []
    for row in rows:
        try:
            items.append(
                PlatformResponse(
                    name=row["name"],
                    auth_status=row["auth_status"],
                    auth_method=row["auth_method"],
                    auth_expires_at=row["auth_expires_at"],
                    last_sync_at=row["last_sync_at"],
                    last_error=(
                        row["last_error"][:_LAST_ERROR_TRUNCATE] if row["last_error"] else None
                    ),
                )
            )
        except ValidationError as e:
            _log.warning(
                "api.platforms.row_dropped",
                name=row["name"],
                reason="response_model_validation_failed",
                errors=[{"loc": err["loc"], "type": err["type"]} for err in e.errors()],
            )

    body = PlatformListResponse(
        platforms=items,
        meta=PlatformsMeta(total=len(items)),
    )
    return JSONResponse(content=body.model_dump())
