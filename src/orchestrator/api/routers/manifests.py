"""GET /api/v1/manifests — paginated list of manifests (BL9 / Feature 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api._query_helpers import (
    FilterAllowList,
    FilterFieldSpec,
    IncludeAllowList,
    QueryParamError,
    SortAllowList,
    SortFieldResponse,
    build_order_by_clause,
    build_where_clause,
    parse_filters,
    parse_includes,
    parse_pagination,
    parse_sort,
)
from orchestrator.api._query_helpers import SortField as _SortField
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool


# Spec D1, D2 constants
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

# Default sort per spec D2: fetched_at:desc. Server-appended id:asc
# tie-breaker (UAT-4 S2-B); applied because user doesn't sort by id by default.
DEFAULT_SORT = (_SortField(field="fetched_at", direction="desc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

MANIFESTS_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "game_id": FilterFieldSpec(ops={"eq", "in"}, value_type=int),
        "version": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "fetched_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
        "chunk_count": FilterFieldSpec(ops={"gte", "lte"}, value_type=int),
        "total_bytes": FilterFieldSpec(ops={"gte", "lte"}, value_type=int),
    }
)

MANIFESTS_SORT_ALLOW_LIST = SortAllowList(
    fields={"id", "game_id", "version", "fetched_at", "chunk_count", "total_bytes"}
)

# Spec D5: the only opt-in expansion key for manifests is "game".
MANIFESTS_INCLUDE_ALLOW_LIST = IncludeAllowList(keys={"game"})

# Manifests columns selected from the manifests table (excludes raw BLOB per spec D1).
# All identifiers are SAFE — sourced from this constant, not user input.
_MANIFEST_COLUMNS = "id, game_id, version, fetched_at, chunk_count, total_bytes, depot_id"

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class GameSummary(BaseModel):
    """Inline game summary populated when ?include=game (spec D6)."""

    model_config = ConfigDict(extra="forbid")
    title: str
    platform: Literal["steam", "epic"]
    app_id: str


class ManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    game_id: int
    version: str
    fetched_at: str
    chunk_count: int
    total_bytes: int
    # depot_id (#127): added in migration 0003 and populated by BL12 manifest
    # fetch. Nullable for rows written before the column existed.
    depot_id: int | None
    # Spec D4: always-present field; populated iff ?include=game was requested.
    game: GameSummary | None


class ManifestsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, dict[str, Any]]  # plain dict per UAT-4 S2-A
    applied_sort: list[SortFieldResponse]
    # Spec D8: list of include keys actually applied (deduped + sorted).
    applied_includes: list[str]


class ManifestListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifests: list[ManifestResponse]
    meta: ManifestsMeta


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/v1", tags=["manifests"])


@router.get(
    "/manifests",
    response_model=ManifestListResponse,
    responses={
        200: {"description": "Paginated list of manifests"},
        400: {"description": "Bad query parameters"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List manifests",
    description=(
        "Returns the manifests feed with filter, sort, pagination, and optional "
        "?include=game inline expansion. Default sort is fetched_at:desc "
        "(matches idx_manifests_game_fetched). The `raw` BLOB column is "
        "intentionally excluded from the response surface."
    ),
)
async def list_manifests(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    try:
        pagination = parse_pagination(
            request.query_params,
            default_limit=DEFAULT_LIMIT,
            max_limit=MAX_LIMIT,
        )
        filters = parse_filters(request.query_params, allow_list=MANIFESTS_FILTER_ALLOW_LIST)
        sort = parse_sort(
            request.query_params,
            allow_list=MANIFESTS_SORT_ALLOW_LIST,
            default=list(DEFAULT_SORT),
            tie_breaker=TIE_BREAKER,
        )
        includes = parse_includes(request.query_params, allow_list=MANIFESTS_INCLUDE_ALLOW_LIST)
    except QueryParamError as e:
        return JSONResponse(content={"detail": str(e)}, status_code=400)

    # Build WHERE / ORDER BY from helpers. Filter/sort field names come
    # from the allow-list-validated manifests-scoped set; SQL builders emit
    # unqualified identifiers, matching all other endpoints. game expansion
    # is a separate follow-up query (no JOIN) — avoids ambiguous `id`
    # under JOIN and keeps the SQL builders source-of-truth conventional.
    where_sql, where_params = build_where_clause(filters, allow_list=MANIFESTS_FILTER_ALLOW_LIST)
    order_sql = build_order_by_clause(sort, allow_list=MANIFESTS_SORT_ALLOW_LIST)

    # nosem: S608 — identifiers from allow-list-validated literals only;
    # values are parameterized via `?`. See _query_helpers security invariants.
    count_sql = f"SELECT COUNT(*) AS total FROM manifests {where_sql}".strip()  # noqa: S608
    rows_sql = (
        f"SELECT {_MANIFEST_COLUMNS} FROM manifests {where_sql} {order_sql} "  # noqa: S608
        f"LIMIT ? OFFSET ?"
    ).strip()
    rows_params = [*where_params, pagination.limit, pagination.offset]

    try:
        count_row = await pool.read_one(count_sql, where_params)
        rows = await pool.read_all(rows_sql, rows_params)
    except PoolError as e:
        _log.error("api.manifests.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)

    total = int(count_row["total"]) if count_row else 0

    # Spec D7: opt-in game expansion is a separate lookup keyed by the
    # distinct game_ids on the current page. games.id is the FK target of
    # manifests.game_id (NOT NULL); ON DELETE CASCADE guarantees no orphans,
    # so every game_id in the page maps to exactly one games row.
    games_by_id: dict[int, GameSummary] = {}
    if "game" in includes and rows:
        distinct_game_ids = sorted({row["game_id"] for row in rows})
        placeholders = ", ".join("?" for _ in distinct_game_ids)
        games_sql = f"SELECT id, title, platform, app_id FROM games WHERE id IN ({placeholders})"  # noqa: S608
        try:
            game_rows = await pool.read_all(games_sql, list(distinct_game_ids))
        except PoolError as e:
            _log.error("api.manifests.read_failed", reason=str(e))
            return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
        for grow in game_rows:
            games_by_id[grow["id"]] = GameSummary(
                title=grow["title"],
                platform=grow["platform"],
                app_id=grow["app_id"],
            )

    manifests: list[ManifestResponse] = []
    for row in rows:
        game: GameSummary | None = None
        if "game" in includes:
            game = games_by_id.get(row["game_id"])
        manifests.append(
            ManifestResponse(
                id=row["id"],
                game_id=row["game_id"],
                version=row["version"],
                fetched_at=row["fetched_at"],
                chunk_count=row["chunk_count"],
                total_bytes=row["total_bytes"],
                depot_id=row["depot_id"],
                game=game,
            )
        )

    # UAT-4 S2-A: plain-dict applied_filters from parsed filters directly
    applied_filters: dict[str, dict[str, Any]] = {
        field_name: dict(ops) for field_name, ops in filters.items()
    }
    applied_sort = [SortFieldResponse(field=s.field, direction=s.direction) for s in sort]
    # Spec D8: stable sorted echo
    applied_includes = sorted(includes)

    body = ManifestListResponse(
        manifests=manifests,
        meta=ManifestsMeta(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            has_more=(pagination.offset + len(manifests) < total),
            applied_filters=applied_filters,
            applied_sort=applied_sort,
            applied_includes=applied_includes,
        ),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))
