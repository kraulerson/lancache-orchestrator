"""GET /api/v1/games — paginated list of the games library (BL7 / Feature 9)."""

from __future__ import annotations

import json
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


# Endpoint constants (spec §3.1, §3.4)
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
LAST_ERROR_TRUNCATE = 200
DEFAULT_SORT = (_SortField(field="title", direction="asc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

GAMES_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "platform": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "status": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "owned": FilterFieldSpec(ops={"eq"}, value_type=int),
        "size_bytes": FilterFieldSpec(ops={"eq", "gte", "lte"}, value_type=int),
        # UAT-4 S3-a: timestamp value_type enforces ISO 8601 format on the value
        "last_prefilled_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
        "last_validated_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
    }
)

GAMES_SORT_ALLOW_LIST = SortAllowList(
    fields={"id", "title", "status", "size_bytes", "last_prefilled_at", "last_validated_at"}
)

# All schema columns explicitly listed so the SELECT is stable across
# future migrations (a new column won't accidentally appear in the wire).
_GAMES_COLUMNS = (
    "id, platform, app_id, title, owned, size_bytes, "
    "current_version, cached_version, status, "
    "last_validated_at, last_prefilled_at, last_error, metadata"
)

# UAT-4 S3-e: cap metadata bytes before json.loads to defend against
# billion-laughs-style payloads + bounded memory per row.
_MAX_METADATA_BYTES = 65536  # 64 KiB; realistic typical is <1 KiB

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class GameResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    platform: Literal["steam", "epic"]
    app_id: str
    title: str
    owned: int
    size_bytes: int | None
    current_version: str | None
    cached_version: str | None
    status: Literal[
        "unknown",
        "not_downloaded",
        "up_to_date",
        "pending_update",
        "downloading",
        "validation_failed",
        "blocked",
        "failed",
    ]
    last_validated_at: str | None
    last_prefilled_at: str | None
    last_error: str | None
    metadata: dict[str, Any] | None


class FilterCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    # Full operator surface declared on the model so future endpoints
    # may use any of them; in BL7 only eq/in/gte/lte are permitted by
    # any field's allow-list (see GAMES_FILTER_ALLOW_LIST above).
    eq: Any | None = None
    in_: list[Any] | None = Field(default=None, alias="in")
    gte: Any | None = None
    lte: Any | None = None
    gt: Any | None = None
    lt: Any | None = None
    ne: Any | None = None


class SortFieldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"]


class GamesMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    # UAT-4 S2-A: plain dict shape — `{field: {op: value}}` — to avoid the
    # all-7-op-keys-with-6-nulls FilterCriterion serialization. The
    # FilterCriterion model is kept above only so OpenAPI schema generation
    # documents the valid `op` keys; runtime uses this dict directly.
    applied_filters: dict[str, dict[str, Any]]
    applied_sort: list[SortFieldResponse]


class GameListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    games: list[GameResponse]
    meta: GamesMeta


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/v1", tags=["games"])


@router.get(
    "/games",
    response_model=GameListResponse,
    responses={
        200: {"description": "Paginated list of games"},
        400: {"description": "Bad query parameters"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List games",
    description=(
        "Returns the games library with filter, sort, and pagination. "
        "See spec docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md "
        "for the full per-field filter + sort allow-list and the meta envelope shape."
    ),
)
async def list_games(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    try:
        pagination = parse_pagination(
            request.query_params,
            default_limit=DEFAULT_LIMIT,
            max_limit=MAX_LIMIT,
        )
        filters = parse_filters(request.query_params, allow_list=GAMES_FILTER_ALLOW_LIST)
        sort = parse_sort(
            request.query_params,
            allow_list=GAMES_SORT_ALLOW_LIST,
            default=list(DEFAULT_SORT),
            tie_breaker=TIE_BREAKER,
        )
    except QueryParamError as e:
        return JSONResponse(content={"detail": str(e)}, status_code=400)

    where_sql, where_params = build_where_clause(filters, allow_list=GAMES_FILTER_ALLOW_LIST)
    # UAT-4 S3-b: pass allow_list to build_order_by_clause for the defensive
    # re-check of field names, matching build_where_clause symmetry.
    order_sql = build_order_by_clause(sort, allow_list=GAMES_SORT_ALLOW_LIST)

    # nosem: S608 — where_sql + order_sql are built from allow-list-validated
    # field names only; user values flow through `?` placeholders. See
    # _query_helpers security invariants and the Hypothesis property test
    # in tests/api/test_query_helpers.py::TestSqlInjectionResistance.
    count_sql = f"SELECT COUNT(*) AS total FROM games {where_sql}".strip()  # noqa: S608
    rows_sql = (
        f"SELECT {_GAMES_COLUMNS} FROM games {where_sql} {order_sql} LIMIT ? OFFSET ?"  # noqa: S608
    ).strip()
    rows_params = [*where_params, pagination.limit, pagination.offset]

    try:
        count_row = await pool.read_one(count_sql, where_params)
        rows = await pool.read_all(rows_sql, rows_params)
    except PoolError as e:
        _log.error("api.games.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)

    total = int(count_row["total"]) if count_row else 0

    games: list[GameResponse] = []
    for row in rows:
        raw_meta = row["metadata"]
        metadata: dict[str, Any] | None
        if raw_meta is None:
            metadata = None
        elif len(raw_meta) > _MAX_METADATA_BYTES:
            # UAT-4 S3-e: size-cap short-circuit before json.loads
            _log.warning(
                "api.games.metadata_oversized",
                game_id=row["id"],
                size_bytes=len(raw_meta),
                cap=_MAX_METADATA_BYTES,
            )
            metadata = None
        else:
            try:
                parsed = json.loads(raw_meta)
                metadata = parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, TypeError, RecursionError) as e:
                # UAT-4 S3-d: catch RecursionError on deeply-nested JSON;
                # was previously uncaught → 500 from the router.
                _log.warning(
                    "api.games.metadata_parse_failed",
                    game_id=row["id"],
                    reason=type(e).__name__,
                )
                metadata = None

        raw_err = row["last_error"]
        last_error = raw_err[:LAST_ERROR_TRUNCATE] if raw_err else None

        games.append(
            GameResponse(
                id=row["id"],
                platform=row["platform"],
                app_id=row["app_id"],
                title=row["title"],
                owned=row["owned"],
                size_bytes=row["size_bytes"],
                current_version=row["current_version"],
                cached_version=row["cached_version"],
                status=row["status"],
                last_validated_at=row["last_validated_at"],
                last_prefilled_at=row["last_prefilled_at"],
                last_error=last_error,
                metadata=metadata,
            )
        )

    # UAT-4 S2-A: build applied_filters as a plain dict matching the parsed
    # `{field: {op: value}}` shape. Previously this went through a
    # FilterCriterion Pydantic model whose model_dump emitted all 7 op
    # keys per field with 6 nulls — contract drift from spec §3.2.
    # FilterCriterion remains in the response model for OpenAPI schema
    # documentation, but the runtime path emits compact dicts directly.
    applied_filters: dict[str, dict[str, Any]] = {
        field_name: dict(ops) for field_name, ops in filters.items()
    }

    applied_sort = [SortFieldResponse(field=s.field, direction=s.direction) for s in sort]

    body = GameListResponse(
        games=games,
        meta=GamesMeta(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            has_more=(pagination.offset + len(games) < total),
            applied_filters=applied_filters,
            applied_sort=applied_sort,
        ),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))
