# BL8-F9 — `GET /api/v1/jobs` (read-only, paginated) — Design Spec

**Date:** 2026-05-20
**Phase:** 2 (Construction), Milestone B, Build Loop 8
**Feature:** F9 partial — second paginated F9 read endpoint
**Branch:** `feat/bl8-jobs-readonly`
**Depends on:** BL5 (FastAPI skeleton), BL6 (envelope conventions), BL7 (`_query_helpers.py`), UAT-4 (helpers hardening)
**Validates:** the proposition that paginated F9 endpoints can be added cheaply by composing the BL7+UAT-4 conventions.

---

## 1. Goal

Ship the second paginated F9 read endpoint. Game_shelf's primary use cases:

- **Jobs feed:** default panel — recent activity, latest first
- **Active-jobs counter:** filtered query `?state_in=queued,running`
- **Per-game history:** `?game_id=N`
- **Audit by source:** `?source=cli` etc.

Operator CLI use cases:
- "What ran today" — `?started_at_gte=YYYY-MM-DD`
- "Failures" — `?state=failed`
- "Almost-done long-runners" — `?state=running&progress_gte=0.9`

This BL also **proves the BL7+UAT-4 convention library propagates cheaply**. No changes to `_query_helpers.py` are required.

---

## 2. Locked decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| **D1** | Default sort | **`id:desc`** | Most-recently-created first; no NULL surprises (timestamps can be NULL); active jobs surface via explicit `?state_in=queued,running` filter (Game_shelf needs this filter for the active-jobs counter anyway). Server-appended `id:asc` tie-breaker is deduped (user explicit `id:desc` wins) → `applied_sort` echo is single-entry `[{id:desc}]`. |
| **D2** | `payload` column | **Include as parsed JSON** | Schema comment is explicit: "NEVER contains credentials." Operator debugging needs visibility into job-internal state (depot lists, sync endpoints, etc.). UAT-4's safety belts apply: 64 KiB raw-bytes cap before `json.loads` (emits `api.jobs.payload_oversized` log + returns null), then `RecursionError` + `JSONDecodeError` + `TypeError` caught (emits `api.jobs.payload_parse_failed` log + returns null). |
| **D3** | `_is_null` operator | **Defer (not in BL8)** | Game_shelf doesn't need it (`?state_in=queued,running` covers "active jobs"). Operator orphan queries are rare. Adding later is non-breaking; opt-in per field. |
| **D4** | Derived fields | **None** | `duration_sec` is trivially client-derivable (`finished_at - started_at`); `age_sec` would introduce `now()`-dependence breaking response determinism. Additive if a real need surfaces. |
| **D5** | `error` truncation | **200 chars at API layer** | BL6/BL7 `last_error` pattern. Defense-in-depth against upstream code accidentally writing sensitive content into the column. |
| **D6** | Filter syntax | **Inherited** from `_query_helpers.py` (operator-suffix per-field allow-list) | Per-endpoint allow-list table in §3.1. |
| **D7** | Sort syntax | **Inherited** (multi-field comma-separated; server-appended `id:asc` tie-breaker; UAT-4 S2-B dedup) | Default `id:desc` per D1. |
| **D8** | Pagination | **Inherited** (offset-based, default 50, max 500, reject 400) | UAT-4 hardened. |
| **D9** | `meta` envelope | **Inherited** (`total`, `limit`, `offset`, `has_more`, `applied_filters` plain-dict, `applied_sort`) | UAT-4 S2-A fix applies. |
| **D10** | Empty result | **Inherited** (200 with empty array + `meta.total=0`) | |
| **D11** | Auth | **Bearer required** (NOT in `AUTH_EXEMPT_PATHS`) | |
| **D12** | Pool error | **Inherited** (`PoolError` → 503 with structured `api.jobs.read_failed` log) | |
| **D13** | Unknown filter/sort | **Inherited** (400 with `{"detail": "..."}`) | |
| **D14** | Pydantic strictness | **Inherited** (`extra="forbid"` on all response models) | |

---

## 3. Wire format

### 3.1 Request

```
GET /api/v1/jobs?<query-params>
Authorization: Bearer <token>
```

**Pagination:** `limit` (default 50, max 500), `offset` (default 0). Inherited.

**Per-endpoint filter allow-list:**

| Field | `=` | `_in` | `_gte` | `_lte` | Value type / Format |
|---|:-:|:-:|:-:|:-:|---|
| `kind` | ✓ | ✓ | | | enum: `prefill`, `validate`, `library_sync`, `auth_refresh`, `sweep` |
| `game_id` | ✓ | | | | int |
| `platform` | ✓ | ✓ | | | enum: `steam`, `epic` |
| `state` | ✓ | ✓ | | | enum: `queued`, `running`, `succeeded`, `failed`, `cancelled` |
| `progress` | | | ✓ | ✓ | float 0.0–1.0 |
| `source` | ✓ | ✓ | | | enum: `scheduler`, `cli`, `gameshelf`, `api` |
| `started_at` | | | ✓ | ✓ | ISO 8601 timestamp (typed-string validator) |
| `finished_at` | | | ✓ | ✓ | ISO 8601 timestamp (typed-string validator) |

**Sortable fields:** `id`, `kind`, `state`, `progress`, `started_at`, `finished_at`.

**Default sort:** `id:desc` (D1).

### 3.2 Response — 200 OK

```json
{
  "jobs": [
    {
      "id": 1234,
      "kind": "prefill",
      "game_id": 42,
      "platform": "steam",
      "state": "running",
      "progress": 0.73,
      "source": "scheduler",
      "started_at": "2026-05-20T13:00:00Z",
      "finished_at": null,
      "error": null,
      "payload": {"depots": [101, 102], "bytes_total": 50000000000}
    }
  ],
  "meta": {
    "total": 487,
    "limit": 50,
    "offset": 0,
    "has_more": true,
    "applied_filters": {
      "state": {"in": ["queued", "running"]}
    },
    "applied_sort": [
      {"field": "id", "direction": "desc"}
    ]
  }
}
```

**Per-job fields** (all 11 schema columns):
- `id`, `kind`, `game_id`, `platform`, `state`, `progress`, `source`
- `started_at`, `finished_at` (ISO 8601 strings, nullable)
- `error` (truncated to 200 chars; null when absent)
- `payload` (parsed JSON dict; null when absent, parse-failed, or oversized)

### 3.3 Error responses

| Status | Body | When |
|---|---|---|
| 400 | `{"detail": "unknown filter field: foo"}` | Query param outside allow-list |
| 400 | `{"detail": "unknown operator: foo for field bar"}` | Operator outside per-field allow-list |
| 400 | `{"detail": "invalid value for progress_gte: 'abc' (...)"}` | Value parse failure |
| 400 | `{"detail": "limit must be <= 500, got X"}` | Pagination overflow |
| 401 | (handled by `BearerAuthMiddleware`) | Missing/invalid bearer |
| 503 | `{"detail": "database unavailable"}` | `PoolError` caught at router |

### 3.4 Examples

```
# Game_shelf default panel — latest activity
GET /api/v1/jobs

# Active-jobs counter (Game_shelf hot path)
GET /api/v1/jobs?state_in=queued,running

# Per-game history
GET /api/v1/jobs?game_id=42&sort=started_at:desc

# Failures in last week
GET /api/v1/jobs?state=failed&started_at_gte=2026-05-13

# Almost-done long-runners
GET /api/v1/jobs?state=running&progress_gte=0.9

# Audit jobs initiated from CLI
GET /api/v1/jobs?source=cli&sort=started_at:desc&limit=100
```

---

## 4. Architecture

### 4.1 File layout

```
src/orchestrator/api/routers/jobs.py             ~190 LoC  (new — handler + Pydantic models + allow-lists)
src/orchestrator/api/main.py                     +2 lines (import + include_router)
tests/api/conftest.py                            +1 fixture (jobs_pool_seeded)
tests/api/test_jobs_router.py                    ~480 LoC, ~25 tests
docs/security-audits/bl8-f9-jobs-readonly-security-audit.md   (audit doc)
```

**No `_query_helpers.py` changes.** Every BL8 capability composes from existing primitives.

### 4.2 Module: `src/orchestrator/api/routers/jobs.py`

```python
"""GET /api/v1/jobs — paginated list of orchestrator jobs (BL8 / Feature 9)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api._query_helpers import (
    FilterAllowList,
    FilterFieldSpec,
    QueryParamError,
    SortAllowList,
)
from orchestrator.api._query_helpers import SortField as _SortField
from orchestrator.api._query_helpers import (
    build_order_by_clause,
    build_where_clause,
    parse_filters,
    parse_pagination,
    parse_sort,
)
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool


DEFAULT_LIMIT = 50
MAX_LIMIT = 500
ERROR_TRUNCATE = 200
PAYLOAD_MAX_BYTES = 65536  # 64 KiB (UAT-4 S3-e parity)

# Default sort per spec D1: id:desc. User explicit "id" in either direction
# deduplicates the tie-breaker append (UAT-4 S2-B behavior).
DEFAULT_SORT = (_SortField(field="id", direction="desc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

JOBS_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "kind":         FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "game_id":      FilterFieldSpec(ops={"eq"},       value_type=int),
        "platform":     FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "state":        FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "progress":     FilterFieldSpec(ops={"gte", "lte"}, value_type=float),
        "source":       FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "started_at":   FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
        "finished_at":  FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
    }
)

JOBS_SORT_ALLOW_LIST = SortAllowList(
    fields={"id", "kind", "state", "progress", "started_at", "finished_at"}
)

_JOBS_COLUMNS = (
    "id, kind, game_id, platform, state, progress, source, "
    "started_at, finished_at, error, payload"
)

_log = structlog.get_logger(__name__)


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    kind: Literal["prefill", "validate", "library_sync", "auth_refresh", "sweep"]
    game_id: int | None
    platform: Literal["steam", "epic"] | None
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: float | None
    source: Literal["scheduler", "cli", "gameshelf", "api"]
    started_at: str | None
    finished_at: str | None
    error: str | None
    payload: dict[str, Any] | None


class SortFieldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"]


class JobsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, dict[str, Any]]   # plain dict per UAT-4 S2-A
    applied_sort: list[SortFieldResponse]


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jobs: list[JobResponse]
    meta: JobsMeta


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
        # payload: parse JSON column; null on absence, oversize, or parse failure
        raw_payload = row["payload"]
        payload: dict[str, Any] | None
        if raw_payload is None:
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
        err = raw_err[:ERROR_TRUNCATE] if raw_err else None

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

    # UAT-4 S2-A: applied_filters as plain dict directly from parsed filters
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
```

### 4.3 SQL strategy

Same two-query pattern as BL7:
1. `SELECT COUNT(*) AS total FROM jobs <where>` — produces `meta.total`
2. `SELECT <columns> FROM jobs <where> <order_by> LIMIT ? OFFSET ?` — produces rows

Both reuse the same `where_sql` + `where_params`. Identifier interpolation only from allow-list-validated literals; values flow ONLY through `?` placeholders.

**Index utilization analysis:**

| Filter / Sort | Index used | Expected perf |
|---|---|---|
| `?state=...` | `idx_jobs_state_kind` | fast (index seek) |
| `?state=...&kind=...` | `idx_jobs_state_kind` (covering) | fast |
| `?sort=started_at:desc` | `idx_jobs_started` (partial, DESC) | fast for rows with non-null `started_at` |
| `?game_id=X&kind=Y&state_in=queued,running` | `idx_jobs_dedupe` (partial) | fast for the dedupe-lookup hot path |
| `?sort=id:desc` (default) | PK | fast |
| `?source=...` | full table scan (no index) | acceptable at expected scale |

No new indexes required for BL8. If `?source=...` becomes a hot path operationally, `CREATE INDEX idx_jobs_source` is a future migration.

### 4.4 Wiring in `main.py`

```python
from orchestrator.api.routers.jobs import router as jobs_router
# ...
app.include_router(health_router)
app.include_router(platforms_router)
app.include_router(games_router)
app.include_router(jobs_router)  # BL8
```

---

## 5. Test plan

Target: ≥95% branch coverage on `routers/jobs.py`. ~25 tests across 7 classes.

### 5.1 `tests/api/conftest.py` — new fixture

```python
@pytest_asyncio.fixture
async def jobs_pool_seeded(populated_pool):
    """populated_pool seeded with ~50 jobs across kinds/states/sources.

    Mix:
    - 5 kinds × 5 states = 25 combinations + duplicates
    - source mix: scheduler (60%), cli (20%), gameshelf (10%), api (10%)
    - timestamps: queued has both NULL; running has started_at only;
      terminal states have both
    - progress: NULL for queued; partial for running; 1.0 for succeeded;
      partial for failed/cancelled
    - error: populated only for failed jobs
    - payload: small JSON object on most; null on a few; one oversized (>64 KiB)
      to exercise the cap; one malformed to exercise the parse-fail path
    """
    ...
```

### 5.2 `tests/api/test_jobs_router.py` — ~25 tests

| Class | Tests |
|---|---|
| `TestJobsEmptyDb` | empty → 200 + empty array + total=0 |
| `TestJobsHappyPath` | seeded jobs returned; 11-field set; envelope shape |
| `TestJobsPagination` | default 50; explicit limit; offset progression; limit > 500 → 400; negative offset → 400; has_more correct |
| `TestJobsFilters` | each allow-listed field × operator: kind (eq, _in); game_id (eq); platform (eq, _in); state (eq, _in); progress (_gte, _lte, range); source (eq, _in); timestamp ranges (_gte, _lte for started_at + finished_at) |
| `TestJobsSort` | default `id:desc`; explicit other sorts; tie-breaker appended on non-id sort; user `id:desc` does NOT append `id:asc` tie-breaker |
| `TestJobsAppliedEcho` | applied_filters compact dict shape (UAT-4 S2-A regression); applied_sort includes tie-breaker correctly per D1 |
| `TestJobsPayloadAndError` | well-formed payload parsed; null payload → null; oversized (>64 KiB) → null + log; malformed JSON → null + log; error truncated to 200 chars; null error → null |
| `TestJobsErrorPaths` | unknown filter field → 400; unknown op → 400; invalid value → 400 (incl. timestamp format); unauth → 401; PoolError → 503 |

### 5.3 What's not tested explicitly (covered by `_query_helpers` tests + UAT-4 regression)

- Identifier validation at allow-list construction (UAT-4 tests cover the helper)
- INT64 range check on `game_id` (UAT-4 covers via _coerce_value)
- `_in` cardinality cap (UAT-4 covers in helper)
- SQL injection resistance (Hypothesis property test in `test_query_helpers.py`)

---

## 6. Risk register

| Risk | Mitigation |
|---|---|
| `payload` schema-comment promise ("NEVER contains credentials") gets violated by upstream code | Defense-in-depth: 64 KiB cap + parse error handling means a malformed/oversized payload returns null instead of crashing. Schema comment is the contract; violations are upstream bugs to fix at the source. |
| Future jobs migration adds a column | `extra="forbid"` on `JobResponse` raises during construction → caught by CI |
| Future `kind`/`state`/`source`/`platform` enum values added | `Literal[...]` rejects unknowns. Migration PR MUST update the model in the same commit. (Document this convention in CHANGELOG.) |
| `idx_jobs_started` is partial (`WHERE started_at IS NOT NULL`) — `?sort=started_at:desc` on default (no filter) does NOT use the partial index | At expected scale (thousands), full table scan + sort is acceptable. UAT-4 S3-f learning applied. |
| `idx_jobs_finished` is partial AND filters on `error IS NULL` (only successful finished jobs) — odd shape, narrow utility | Not BL8's concern; the index exists for the future "prune-old-successes" sweep job |
| `progress` REAL comparisons can be lossy at exact-equality boundaries | Allow-list permits only `_gte`/`_lte` (no `eq`); range queries are precise enough |
| Concurrent writes drift `total` vs row set across the two queries | UAT-4 S3-g: documented as expected behavior for single-orchestrator. Acceptable for MVP. |
| Game_shelf needs orphan-job introspection (`game_id IS NULL`) before `_is_null` lands | Workaround: direct DB query. Re-evaluate `_is_null` if Game_shelf surfaces a real need. |

---

## 7. Documentation deltas

- **CHANGELOG.md** — add to `[Unreleased]` → `### Added` (BL8 entry)
- **FEATURES.md** — new Feature 8 entry
- **Security audit** — `docs/security-audits/bl8-f9-jobs-readonly-security-audit.md`
- **ADR** — none. This spec + CHANGELOG entry are the design record; ADR is for cross-cutting decisions.

---

## 8. Cross-references

- **Spec ancestor:** BL7 spec `docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md` (conventions inherited)
- **UAT-4 closure:** `docs/security-audits/uat-4-remediation-security-audit.md` (12 fixes that BL8 inherits for free)
- **Data model:** `docs/phase-1/data-model.md` (jobs table schema + invariants)
- **API substrate:** ADR-0012 + UAT-3 addendum
- **Pool API:** ADR-0011
- **Bible:** §8 (observability), §10.5 (Depends pattern)

---

## 9. Open follow-ups (deferred, not blocking)

- **`_is_null` operator** — defer until a clear consumer need (per D3)
- **Per-job endpoint `GET /api/v1/jobs/{id}`** — clients read the list; if a real need surfaces, additive
- **Cursor-based pagination mode** — additive if/when retention grows to millions of rows
- **`idx_jobs_source` index** — only if profiling shows `?source=...` is a hot slow path
- **Job kind/state Literal-vs-migration sync** — formalize the "update Literal in same PR" rule as a contributor note when the first migration adds a new enum value
- **Derived `duration_sec`** — add if Game_shelf builds a "how long this took" UI that wants pre-computed values
