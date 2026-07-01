"""GET /api/v1/jobs — paginated list of orchestrator jobs (BL8 / Feature 9)."""

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
from orchestrator.api._query_helpers import SortField as _SortField
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool


# Spec D1, D5 constants
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
PAYLOAD_MAX_BYTES = 65536  # 64 KiB (UAT-4 S3-e parity)

# Default sort per spec D1: id:desc. User explicit "id" in either direction
# deduplicates the tie-breaker append (UAT-4 S2-B behavior).
DEFAULT_SORT = (_SortField(field="id", direction="desc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

JOBS_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "kind": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "game_id": FilterFieldSpec(ops={"eq"}, value_type=int),
        "platform": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "state": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "progress": FilterFieldSpec(ops={"gte", "lte"}, value_type=float),
        "source": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "started_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
        "finished_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
    }
)

JOBS_SORT_ALLOW_LIST = SortAllowList(
    fields={"id", "kind", "state", "progress", "started_at", "finished_at"}
)

# UAT-5 U5-8: enforce ?include= convention (no includable keys today on jobs).
JOBS_INCLUDE_ALLOW_LIST = IncludeAllowList(keys=set())

# All schema columns listed explicitly so the SELECT is stable across
# future migrations.
_JOBS_COLUMNS = (
    "id, kind, game_id, platform, state, progress, source, started_at, finished_at, error, payload"
)

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    kind: Literal[
        "prefill",
        "validate",
        "library_sync",
        "auth_refresh",
        "sweep",
        "manifest_fetch",
        "fetch_manifests",
    ]
    game_id: int | None
    platform: Literal["steam", "epic"] | None
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: float | None
    source: Literal["scheduler", "cli", "gameshelf", "api"]
    started_at: str | None
    finished_at: str | None
    error: str | None
    payload: dict[str, Any] | None


class JobsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, dict[str, Any]]  # plain dict per UAT-4 S2-A
    applied_sort: list[SortFieldResponse]


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jobs: list[JobResponse]
    meta: JobsMeta


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/v1", tags=["jobs"])


@router.get(
    "/jobs",
    response_model=JobListResponse,
    responses={
        200: {"description": "Paginated list of jobs"},
        400: {"description": "Bad query parameters"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List jobs",
    description=(
        "Returns the orchestrator jobs feed with filter, sort, and pagination. "
        "Default sort is id:desc (most recently created). Active jobs surface "
        "via ?state_in=queued,running. See spec "
        "docs/superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md "
        "for the full per-field filter + sort allow-list."
    ),
)
async def list_jobs(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    try:
        pagination = parse_pagination(
            request.query_params,
            default_limit=DEFAULT_LIMIT,
            max_limit=MAX_LIMIT,
        )
        filters = parse_filters(request.query_params, allow_list=JOBS_FILTER_ALLOW_LIST)
        sort = parse_sort(
            request.query_params,
            allow_list=JOBS_SORT_ALLOW_LIST,
            default=list(DEFAULT_SORT),
            tie_breaker=TIE_BREAKER,
        )
        # UAT-5 U5-8: enforce ?include= rejection on jobs (no includable keys).
        parse_includes(request.query_params, allow_list=JOBS_INCLUDE_ALLOW_LIST)
    except QueryParamError as e:
        return JSONResponse(content={"detail": str(e)}, status_code=400)

    where_sql, where_params = build_where_clause(filters, allow_list=JOBS_FILTER_ALLOW_LIST)
    order_sql = build_order_by_clause(sort, allow_list=JOBS_SORT_ALLOW_LIST)

    # nosem: S608 — identifiers from allow-list-validated literals only;
    # values are parameterized via `?`. See _query_helpers security invariants.
    count_sql = f"SELECT COUNT(*) AS total FROM jobs {where_sql}".strip()  # noqa: S608
    rows_sql = (
        f"SELECT {_JOBS_COLUMNS} FROM jobs {where_sql} {order_sql} LIMIT ? OFFSET ?"  # noqa: S608
    ).strip()
    rows_params = [*where_params, pagination.limit, pagination.offset]

    try:
        count_row = await pool.read_one(count_sql, where_params)
        rows = await pool.read_all(rows_sql, rows_params)
    except PoolError as e:
        _log.error("api.jobs.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)

    total = int(count_row["total"]) if count_row else 0

    jobs: list[JobResponse] = []
    for row in rows:
        raw_payload = row["payload"]
        payload: dict[str, Any] | None
        if raw_payload is None:
            payload = None
        elif not isinstance(raw_payload, (str, bytes, bytearray)):
            # UAT-5 U5-3: defensive guard against non-buffer pool returns.
            _log.warning(
                "api.jobs.payload_unexpected_type",
                job_id=row["id"],
                value_type=type(raw_payload).__name__,
            )
            payload = None
        elif len(raw_payload) > PAYLOAD_MAX_BYTES:
            _log.warning(
                "api.jobs.payload_oversized",
                job_id=row["id"],
                size_bytes=len(raw_payload),
                cap=PAYLOAD_MAX_BYTES,
            )
            payload = None
        else:
            try:
                parsed = json.loads(raw_payload)
                payload = parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, TypeError, RecursionError) as e:
                _log.warning(
                    "api.jobs.payload_parse_failed",
                    job_id=row["id"],
                    reason=type(e).__name__,
                )
                payload = None

        raw_err = row["error"]
        err = raw_err[:ERROR_TRUNCATE_BYTES] if raw_err else None

        # UAT-5 U5-2: wrap per-row response construction. Pydantic Literal[]
        # fields (kind, platform, state, source) raise ValidationError if the
        # DB row holds an out-of-allow-list value. Skip with a structured log
        # rather than 500-crashing the whole request.
        try:
            jobs.append(
                JobResponse(
                    id=row["id"],
                    kind=row["kind"],
                    game_id=row["game_id"],
                    platform=row["platform"],
                    state=row["state"],
                    progress=row["progress"],
                    source=row["source"],
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                    error=err,
                    payload=payload,
                )
            )
        except ValidationError as e:
            _log.warning(
                "api.jobs.row_dropped",
                job_id=row["id"],
                reason="response_model_validation_failed",
                errors=[{"loc": err["loc"], "type": err["type"]} for err in e.errors()],
            )

    # UAT-4 S2-A: plain-dict applied_filters from parsed filters directly
    applied_filters: dict[str, dict[str, Any]] = {
        field_name: dict(ops) for field_name, ops in filters.items()
    }
    applied_sort = [SortFieldResponse(field=s.field, direction=s.direction) for s in sort]

    body = JobListResponse(
        jobs=jobs,
        meta=JobsMeta(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            has_more=(pagination.offset + len(jobs) < total),
            applied_filters=applied_filters,
            applied_sort=applied_sort,
        ),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))
