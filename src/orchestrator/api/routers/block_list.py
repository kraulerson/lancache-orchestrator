"""Block-list REST resource (F8). GET (paginated) / POST (idempotent) / DELETE.

`block_list` is the single source of truth for "skip during scheduled prefill".
POST accepts an unknown ``(platform, app_id)`` so an app can be pre-blocked
before the orchestrator has enumerated it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api._query_helpers import (
    FilterAllowList,
    FilterFieldSpec,
    QueryParamError,
    SortAllowList,
    SortFieldResponse,
    build_order_by_clause,
    build_where_clause,
    parse_filters,
    parse_pagination,
    parse_sort,
)
from orchestrator.api._query_helpers import (
    SortField as _SortField,
)
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["block_list"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
DEFAULT_SORT = (_SortField(field="blocked_at", direction="desc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

BLOCK_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "platform": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "source": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
    }
)
BLOCK_SORT_ALLOW_LIST = SortAllowList(fields={"blocked_at", "platform", "app_id", "id"})

_COLUMNS = "id, platform, app_id, reason, source, blocked_at"


class BlockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    platform: Literal["steam", "epic"]
    app_id: str
    reason: str | None
    source: Literal["cli", "gameshelf", "api", "config"]
    blocked_at: str


class BlockListMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, dict[str, Any]]
    applied_sort: list[SortFieldResponse]


class BlockListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_list: list[BlockEntry]
    meta: BlockListMeta


class BlockCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["steam", "epic"]
    app_id: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=500)
    source: Literal["cli", "gameshelf", "api", "config"] = "api"


def _entry_kwargs(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "platform": row["platform"],
        "app_id": row["app_id"],
        "reason": row["reason"],
        "source": row["source"],
        "blocked_at": row["blocked_at"],
    }


@router.get("/block-list", response_model=BlockListResponse)
async def list_block_list(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    try:
        pagination = parse_pagination(
            request.query_params, default_limit=DEFAULT_LIMIT, max_limit=MAX_LIMIT
        )
        filters = parse_filters(request.query_params, allow_list=BLOCK_FILTER_ALLOW_LIST)
        sort = parse_sort(
            request.query_params,
            allow_list=BLOCK_SORT_ALLOW_LIST,
            default=list(DEFAULT_SORT),
            tie_breaker=TIE_BREAKER,
        )
    except QueryParamError as e:
        return JSONResponse(content={"detail": str(e)}, status_code=400)

    where_sql, where_params = build_where_clause(filters, allow_list=BLOCK_FILTER_ALLOW_LIST)
    order_sql = build_order_by_clause(sort, allow_list=BLOCK_SORT_ALLOW_LIST)
    # nosem: S608 — where_sql/order_sql are built from allow-list-validated field
    # names only; user values flow through `?` placeholders (see games.py +
    # _query_helpers security invariants).
    count_sql = f"SELECT COUNT(*) AS total FROM block_list {where_sql}".strip()  # noqa: S608
    rows_sql = (
        f"SELECT {_COLUMNS} FROM block_list {where_sql} {order_sql} LIMIT ? OFFSET ?"  # noqa: S608
    ).strip()
    rows_params = [*where_params, pagination.limit, pagination.offset]

    try:
        count_row = await pool.read_one(count_sql, where_params)
        rows = await pool.read_all(rows_sql, rows_params)
    except PoolError as e:
        _log.error("api.block_list.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)

    total = int(count_row["total"]) if count_row else 0
    entries = [BlockEntry(**_entry_kwargs(r)) for r in rows]
    body = BlockListResponse(
        block_list=entries,
        meta=BlockListMeta(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            has_more=(pagination.offset + len(entries) < total),
            applied_filters={f: dict(ops) for f, ops in filters.items()},
            applied_sort=[SortFieldResponse(field=s.field, direction=s.direction) for s in sort],
        ),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))


@router.post(
    "/block-list",
    response_model=BlockEntry,
    responses={200: {"description": "Already blocked"}, 201: {"description": "Blocked"}},
)
async def create_block(
    payload: BlockCreate,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        inserted = await pool.execute_write(
            "INSERT INTO block_list (platform, app_id, reason, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(platform, app_id) DO NOTHING",
            (payload.platform, payload.app_id, payload.reason, payload.source),
        )
        row = await pool.read_one(
            f"SELECT {_COLUMNS} FROM block_list WHERE platform=? AND app_id=?",  # noqa: S608
            (payload.platform, payload.app_id),
        )
    except PoolError as e:
        _log.error("api.block_list.write_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    if row is None:  # pragma: no cover — write succeeded but row vanished
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    entry = BlockEntry(**_entry_kwargs(row))
    return JSONResponse(content=entry.model_dump(), status_code=201 if inserted else 200)


@router.delete("/block-list/{platform}/{app_id}")
async def delete_block(
    platform: str,
    app_id: str,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        removed = await pool.execute_write(
            "DELETE FROM block_list WHERE platform=? AND app_id=?", (platform, app_id)
        )
    except PoolError as e:
        _log.error("api.block_list.delete_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    return JSONResponse(content={"removed": int(removed)}, status_code=200)
