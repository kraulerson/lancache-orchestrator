"""GET /api/v1/games — paginated list of the games library (BL7 / Feature 9)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from orchestrator.api._query_helpers import (
    ERROR_TRUNCATE_BYTES,
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

# UAT-5 U5-8: games doesn't currently support any ?include= expansion, but
# the convention from BL9 (/manifests) is that endpoints declare an
# IncludeAllowList explicitly. With an empty allow-list, any ?include= value
# is rejected with 400. Locks in convention enforcement so typos surface.
GAMES_INCLUDE_ALLOW_LIST = IncludeAllowList(keys=set())

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

# Shared row projection for BOTH the list and the detail endpoint (#141): schema
# columns + `blocked` (correlated EXISTS) + the latest validation_history chunk
# counts (correlated scalar subqueries, newest by started_at with an id DESC
# tie-break so both subqueries pick the same row). Callers append their own
# WHERE/ORDER/LIMIT. One constant so list and detail can never drift in what a
# "game row" contains.
_GAME_ROW_SELECT = (
    f"SELECT {_GAMES_COLUMNS}, "  # noqa: S608  only the static _GAMES_COLUMNS is interpolated
    "EXISTS(SELECT 1 FROM block_list b "
    "WHERE b.platform=games.platform AND b.app_id=games.app_id) AS blocked, "
    "(SELECT vh.chunks_cached FROM validation_history vh "
    " WHERE vh.game_id=games.id ORDER BY vh.started_at DESC, vh.id DESC LIMIT 1) "
    "AS chunks_cached, "
    "(SELECT vh.chunks_total FROM validation_history vh "
    " WHERE vh.game_id=games.id ORDER BY vh.started_at DESC, vh.id DESC LIMIT 1) "
    "AS chunks_total "
    "FROM games"
)


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
    blocked: bool
    # Latest validation_history counts (newest row by started_at) so the UI can
    # render a "Partial · N%" badge without a second round-trip. Both null when
    # the game has never been validated.
    chunks_cached: int | None
    chunks_total: int | None


class GamesMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    # UAT-4 S2-A: plain dict shape — `{field: {op: value}}` — runtime-built
    # from parsed filters directly; no Pydantic wrapper.
    applied_filters: dict[str, dict[str, Any]]
    applied_sort: list[SortFieldResponse]


class GameListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    games: list[GameResponse]
    meta: GamesMeta


class GameDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game: GameResponse


# ---------------------------------------------------------------------------
# Row → response model (shared by list + detail)
# ---------------------------------------------------------------------------


def _row_to_game_response(row: Any) -> GameResponse | None:
    """Build a GameResponse from a games row projected via _GAME_ROW_SELECT.

    Returns None — with a structured log — when the row's metadata is
    malformed/oversized or a Literal column (platform, status) holds an
    out-of-allow-list value, so one bad row never 500s the endpoint. The list
    endpoint skips such rows; the detail endpoint treats a None as a
    data-integrity error (the row exists but can't be rendered).
    """
    raw_meta = row["metadata"]
    metadata: dict[str, Any] | None
    if raw_meta is None:
        metadata = None
    elif not isinstance(raw_meta, (str, bytes, bytearray)):
        # UAT-5 U5-3: defensive guard against future pool drivers that may
        # auto-decode JSON columns to dict/list/int. Treat as malformed.
        _log.warning(
            "api.games.metadata_unexpected_type",
            game_id=row["id"],
            value_type=type(raw_meta).__name__,
        )
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
            # UAT-4 S3-d: catch RecursionError on deeply-nested JSON.
            _log.warning(
                "api.games.metadata_parse_failed",
                game_id=row["id"],
                reason=type(e).__name__,
            )
            metadata = None

    raw_err = row["last_error"]
    last_error = raw_err[:ERROR_TRUNCATE_BYTES] if raw_err else None

    # UAT-5 U5-2: Pydantic Literal[] columns (platform, status) raise
    # ValidationError if the DB row holds an out-of-allow-list value. Skip the
    # row with a structured log instead of propagating a 500.
    try:
        return GameResponse(
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
            blocked=bool(row["blocked"]),
            chunks_cached=row["chunks_cached"],
            chunks_total=row["chunks_total"],
        )
    except ValidationError as e:
        _log.warning(
            "api.games.row_dropped",
            game_id=row["id"],
            reason="response_model_validation_failed",
            errors=[{"loc": err["loc"], "type": err["type"]} for err in e.errors()],
        )
        return None


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
        # UAT-5 U5-8: enforce ?include= convention even though games has no
        # includable keys today. Empty allow-list rejects any ?include=foo
        # with 400 — locks in convention so a typo against the API surfaces
        # rather than being silently ignored.
        parse_includes(request.query_params, allow_list=GAMES_INCLUDE_ALLOW_LIST)
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
    # `blocked` (EXISTS) + latest validation counts (scalar subqueries) live in
    # the shared _GAME_ROW_SELECT projection — NOT a JOIN — so the allow-list
    # `where_sql`/`order_sql` stay on unambiguous bare `games` column names.
    rows_sql = f"{_GAME_ROW_SELECT} {where_sql} {order_sql} LIMIT ? OFFSET ?".strip()
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
        game = _row_to_game_response(row)
        if game is not None:
            games.append(game)

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


@router.get(
    "/games/{game_id}",
    response_model=GameDetailResponse,
    responses={
        200: {"description": "Single game detail"},
        401: {"description": "Missing or invalid bearer token"},
        404: {"description": "No game with that id"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="Get one game",
    description=(
        'Returns a single game by its numeric id, wrapped as `{"game": {...}}`. '
        "The game object carries the same fields as a list row — including the "
        "`blocked` flag and the latest validation chunk counts for a Partial · N% "
        "badge. 404 when no game has that id."
    ),
)
async def get_game(
    game_id: int,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    # game_id flows through a `?` placeholder; the only interpolated fragment is
    # the constant _GAME_ROW_SELECT — no user string reaches the SQL text.
    detail_sql = f"{_GAME_ROW_SELECT} WHERE games.id = ?"
    try:
        row = await pool.read_one(detail_sql, [game_id])
    except PoolError as e:
        _log.error("api.games.detail_read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)

    if row is None:
        return JSONResponse(content={"detail": "game not found"}, status_code=404)

    game = _row_to_game_response(row)
    if game is None:
        # Row exists but can't be rendered (malformed metadata / out-of-allow-list
        # Literal) — a data-integrity error, not a missing resource.
        _log.error("api.games.detail_row_invalid", game_id=game_id)
        return JSONResponse(content={"detail": "game record invalid"}, status_code=500)

    body = GameDetailResponse(game=game)
    return JSONResponse(content=body.model_dump(by_alias=True))
