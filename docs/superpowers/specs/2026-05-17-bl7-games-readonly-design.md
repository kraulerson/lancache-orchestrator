# BL7-F9 — `GET /api/v1/games` (read-only, paginated) — Design Spec

**Date:** 2026-05-17
**Phase:** 2 (Construction), Milestone B, Build Loop 7
**Feature:** F9 partial — first paginated F9 read endpoint
**Branch:** `feat/bl7-games-readonly`
**Depends on:** BL5 (FastAPI skeleton + UAT-3 substrate), BL6 (envelope + error-handling conventions)
**Unblocks:** all future paginated F9 endpoints — `/jobs`, `/manifests`, `/stats`, `/block_list`. The filter/sort/pagination conventions locked here propagate via the shared `_query_helpers.py` module.

---

## 1. Goal

Ship the first paginated F9 read endpoint on the BL5/BL6 substrate. Returns the games library with filter, sort, and pagination. Game_shelf UI is the primary consumer; operator CLI is secondary.

This BL also locks the API conventions every future paginated F9 endpoint will inherit:

- **Pagination model:** offset-based (`limit` + `offset`); 400 if `limit > max`
- **Envelope shape:** wrapped `{"<resource>": [...], "meta": {...}}` with rich meta including `applied_filters` + `applied_sort` echo
- **Filter syntax:** operator-suffix convention (`field`, `field_in`, `field_gte`, `field_lte`)
- **Sort syntax:** comma-separated multi-field with `:asc`/`:desc` direction, server-appended `id:asc` tie-breaker for pagination stability
- **Empty results:** 200 with `{"games": [], "meta": {"total": 0, ...}}` — never 404
- **Per-endpoint allow-list:** filter + sort field/operator allow-list declared per endpoint; unknown field/op → 400
- **Shared parser module:** `_query_helpers.py` (parser, validator, SQL builder) reused across all paginated endpoints

---

## 2. Locked decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| **D1** | Pagination model | **Offset-based** (`limit`, `offset`) | Single-orchestrator dataset (500–50K rows); cursor stability is solving a problem we don't have; `total` enables "Page X of Y" UX trivially; curl-friendly for operator CLI; additive migration to cursor-mode possible per-endpoint if real scale arrives later. |
| **D2** | `meta` envelope shape | **Rich**: `total`, `limit`, `offset`, `has_more`, `applied_filters`, `applied_sort` | Operator's stated preference: "more info than needed > not enough". `applied_filters/applied_sort` echo provides debug + self-documenting contract; structured serialization defined in §4. |
| **D3** | Default + max page size | **default=50, max=500**; clamp behavior: **reject 400** | 50 default matches Game_shelf card-grid UX; 500 max lets operator pull whole library in ~10 requests for backups; loud rejection (not silent clamp) catches operator mistakes immediately. |
| **D4** | Filter syntax | **Operator-suffix**: `field`, `field_in`, `field_gte`, `field_lte`, `field_gt`, `field_lt`, `field_ne` | FastAPI-idiomatic (`Query()` params auto-parse + auto-document); locks one syntax across all paginated F9 endpoints; per-endpoint allow-list acts as security boundary AND docs; equality is no-suffix; multi-value is `_in=a,b,c`. |
| **D5** | Sort syntax | **Comma-separated multi-field with `:asc`/`:desc`**; server-appended `id:asc` tie-breaker | Tie-breaker prevents offset-pagination drift on tied values (a real correctness bug, not just polish). Multi-field supports "newest first, alpha fallback" UX naturally. |
| **D6** | `metadata` column handling | **Include as parsed JSON** | Not sensitive (just depot IDs and build hints — public Steam/Epic plumbing); bounded size; aligns with operator preference for completeness. |
| **D7** | `last_error` handling | **Include but truncate to 200 chars** (BL6 pattern) | Defense-in-depth on top of upstream sync-error scrubbing. Reuses BL6's truncation logic; same 200-char cap. |
| **D8** | Empty result behavior | **200 with empty array** + `meta.total=0` | Collection exists; just contains no matches. REST convention; 404 would conflate "no rows match" with "endpoint doesn't exist". |
| **D9** | Unknown filter/sort field or operator | **400** with `{"detail": "unknown filter field: foo"}` | Loud failure; protects against operator typos that would silently match all rows. |
| **D10** | Pydantic strictness | `extra="forbid"` on response models (BL6 pattern) | Catches schema drift at CI time. |
| **D11** | Auth | **Bearer required** (NOT in `AUTH_EXEMPT_PATHS`) | Standard authenticated read endpoint. |
| **D12** | Pool error handling | **`PoolError` → 503** with structured `api.games.read_failed` log (BL6 pattern) | Consistent with `/health` and `/platforms`. |

---

## 3. Wire format

### 3.1 Request

```
GET /api/v1/games?<query-params>
Authorization: Bearer <token>
```

**Pagination params:**
| Param | Type | Default | Range | Notes |
|---|---|---|---|---|
| `limit` | int | 50 | 1–500 | `limit > 500` → 400 |
| `offset` | int | 0 | ≥0 | `offset < 0` → 400 |

**Filter params (per-field allow-list):**
| Field | `=` | `_in` | `_gte` | `_lte` | Type / Format |
|---|:-:|:-:|:-:|:-:|---|
| `platform` | ✓ | ✓ | | | enum: `steam`, `epic` |
| `status` | ✓ | ✓ | | | enum (8 values per schema) |
| `owned` | ✓ | | | | boolean: 0 or 1 |
| `size_bytes` | ✓ | | ✓ | ✓ | int (bytes) |
| `last_prefilled_at` | | | ✓ | ✓ | ISO 8601 string |
| `last_validated_at` | | | ✓ | ✓ | ISO 8601 string |

Multi-value via `_in`: `?status_in=not_downloaded,pending_update`. Comma-separated. Whitespace stripped per value.

Within a field, multiple criteria AND together (e.g., `size_bytes_gte=1000&size_bytes_lte=5000` = "1KB ≤ size ≤ 5KB"). Across fields, criteria AND together.

**Sort param:**
- Single `sort` param, comma-separated field list
- Each entry: `field` (asc default) or `field:asc` or `field:desc`
- Sortable fields: `id`, `title`, `status`, `size_bytes`, `last_prefilled_at`, `last_validated_at`
- Default: `sort=title:asc`
- Server appends `id:asc` as final tie-breaker, **with de-duplication**: if the user-specified sort already includes `id` (in either direction), the server-appended entry is OMITTED rather than producing a contradictory `ORDER BY id desc, id asc`. The user's explicit `id` ordering wins.

### 3.2 Response — 200 OK

```json
{
  "games": [
    {
      "id": 42,
      "platform": "steam",
      "app_id": "1086940",
      "title": "Baldur's Gate 3",
      "owned": 1,
      "size_bytes": 122000000000,
      "current_version": "manifest_gid_xxx",
      "cached_version": "manifest_gid_xxx",
      "status": "up_to_date",
      "last_validated_at": "2026-05-20T12:34:56Z",
      "last_prefilled_at": "2026-05-19T03:00:00Z",
      "last_error": null,
      "metadata": {"depots": [1086941, 1086942]}
    }
  ],
  "meta": {
    "total": 487,
    "limit": 50,
    "offset": 0,
    "has_more": true,
    "applied_filters": {
      "platform": {"eq": "steam"},
      "size_bytes": {"gte": 1000000000}
    },
    "applied_sort": [
      {"field": "last_prefilled_at", "direction": "desc"},
      {"field": "title", "direction": "asc"},
      {"field": "id", "direction": "asc"}
    ]
  }
}
```

**Per-game fields** (all schema columns; nothing excluded except via truncation):
- `id`, `platform`, `app_id`, `title`, `owned`, `size_bytes`
- `current_version`, `cached_version`, `status`
- `last_validated_at`, `last_prefilled_at`, `last_error` (truncated to 200 chars)
- `metadata` (parsed JSON; `null` if column is `NULL` or JSON parse fails — see §6 risk register)

**`meta` fields:**
| Field | Type | Notes |
|---|---|---|
| `total` | int | Total rows matching filters (after filter, before paginate). Returned via `SELECT COUNT(*)`. |
| `limit` | int | Echo of resolved limit (after defaulting) |
| `offset` | int | Echo of resolved offset |
| `has_more` | bool | `offset + len(games) < total` |
| `applied_filters` | object | `{field: {op: value}}`. `op` is one of `eq`, `in`, `gte`, `lte`. If a field has no filter applied, it's absent. |
| `applied_sort` | array | List of `{field, direction}` in priority order. Always includes server-appended `id:asc` as final entry. |

### 3.3 Error responses

| Status | Body | When |
|---|---|---|
| 400 | `{"detail": "unknown filter field: foo"}` | Query param outside allow-list |
| 400 | `{"detail": "unknown operator: foo for field bar"}` | Operator outside allow-list for the field |
| 400 | `{"detail": "invalid value for size_bytes_gte: 'abc'"}` | Value parse failure (Pydantic) |
| 400 | `{"detail": "invalid sort: 'foo' is not a sortable field"}` | Sort field outside allow-list |
| 400 | `{"detail": "limit must be ≤ 500"}` | `limit > 500` |
| 401 | (handled by `BearerAuthMiddleware`) | Missing/invalid bearer |
| 503 | `{"detail": "database unavailable"}` | `PoolError` caught at router |

### 3.4 Examples

**Default request (no params):**
```
GET /api/v1/games
→ first 50 games, sorted by title:asc, all platforms, all statuses
```

**Pull whole library, alphabetized:**
```
GET /api/v1/games?limit=500
→ first 500 games (default sort, alphabetical)
```

**Game_shelf "Recently Prefilled" panel:**
```
GET /api/v1/games?sort=last_prefilled_at:desc&status_in=up_to_date,pending_update&limit=20
→ 20 most-recently-prefilled games that are cached or due
```

**Operator "what's pending":**
```
GET /api/v1/games?status=not_downloaded&platform=steam
→ all Steam games not yet downloaded
```

**Big games, sorted by size:**
```
GET /api/v1/games?size_bytes_gte=10000000000&sort=size_bytes:desc
→ games larger than 10 GB, biggest first
```

---

## 4. Architecture

### 4.1 File layout

```
src/orchestrator/api/routers/games.py            ~180 LoC  (new — handler + Pydantic models)
src/orchestrator/api/_query_helpers.py           ~200 LoC  (new — parser/validator/builder; reused by future endpoints)
src/orchestrator/api/main.py                     +1 line   (include_router)
tests/api/test_games_router.py                   ~500 LoC, ~30 tests
tests/api/test_query_helpers.py                  ~250 LoC, ~18 tests
```

`_query_helpers.py` is the deliverable that locks the conventions. It MUST be reusable; this means strict scope:

- `parse_pagination(request: Request, max_limit: int) -> PaginationParams` — pulls `limit`/`offset`, validates
- `parse_filters(request: Request, allow_list: FilterAllowList) -> dict[str, dict[str, Any]]` — pulls field+op params, validates against allow-list, returns echo-ready dict
- `parse_sort(request: Request, allow_list: SortAllowList, default: list[SortField], tie_breaker: SortField) -> list[SortField]` — pulls and parses `sort=`, applies default + tie-breaker
- `build_where_clause(filters: dict, allow_list: FilterAllowList) -> tuple[str, list[Any]]` — produces parameterized SQL `WHERE` fragment + ordered param list
- `build_order_by_clause(sort: list[SortField]) -> str` — produces SQL `ORDER BY` fragment from already-validated sort spec

Strict scope rule: NO domain logic in `_query_helpers.py`. If `/jobs` needs different semantics, it gets its own helper. The shared module is for the parser/validator/builder primitives only.

### 4.2 Pydantic models

```python
class GameResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    platform: Literal["steam", "epic"]
    app_id: str
    title: str
    owned: int  # 0 or 1 per schema
    size_bytes: int | None
    current_version: str | None
    cached_version: str | None
    status: Literal[
        "unknown", "not_downloaded", "up_to_date", "pending_update",
        "downloading", "validation_failed", "blocked", "failed",
    ]
    last_validated_at: str | None
    last_prefilled_at: str | None
    last_error: str | None
    metadata: dict[str, Any] | None  # parsed JSON; None if NULL or parse error


class FilterCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Exactly one or more populated per field that's filtered (e.g.,
    # gte+lte for a range). The full operator set is declared on the
    # model so future endpoints can use gt/lt/ne without a Pydantic
    # change — but in BL7 only eq/in/gte/lte are permitted by any
    # field's allow-list per §3.1. The unused fields remain absent
    # from the API surface until a future endpoint permits them.
    eq: Any | None = None
    in_: list[Any] | None = Field(default=None, alias="in")
    gte: Any | None = None
    lte: Any | None = None
    gt: Any | None = None
    lt: Any | None = None
    ne: Any | None = None


class SortField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"]


class GamesMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, FilterCriterion]
    applied_sort: list[SortField]


class GameListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    games: list[GameResponse]
    meta: GamesMeta
```

### 4.3 SQL strategy

Single connection roundtrip per request — TWO statements:

1. `SELECT COUNT(*) FROM games WHERE <filter>` — produces `meta.total`
2. `SELECT <columns> FROM games WHERE <filter> ORDER BY <sort> LIMIT ? OFFSET ?` — produces `games` array

Both reuse the same `WHERE` fragment from `build_where_clause`. Bind parameters are always positional `?`, never string-interpolated.

**Index usage analysis:**
- Filter by `status`: `idx_games_status` — fast
- Filter by `platform`: `idx_games_platform_app` covering — fast
- Filter by `platform + status`: composite scan via `idx_games_status` (sqlite query planner picks the more selective)
- Sort by `last_prefilled_at`: `idx_games_last_prefilled` (partial DESC) — fast for the common "recent" view
- Sort by `title`: full-table sort (no index) — acceptable at 5K-50K rows; sub-100ms in practice
- Sort by `size_bytes`: full-table sort — same

No new indexes proposed in BL7. If `/games?sort=size_bytes` shows up as a slow query in production, add `idx_games_size_bytes` then. Premature index creation is YAGNI.

### 4.4 Wiring in `main.py`

```python
from orchestrator.api.routers.games import router as games_router
# ...
app.include_router(health_router)
app.include_router(platforms_router)
app.include_router(games_router)  # BL7
```

---

## 5. Test plan

Target: ≥95% branch coverage on `routers/games.py` and `_query_helpers.py`.

### 5.1 `tests/api/test_query_helpers.py` (~18 tests)

| Class | Tests |
|---|---|
| `TestParsePagination` | default limit/offset; explicit limit/offset; limit > max → 400; negative offset → 400; non-numeric → 400 |
| `TestParseFilters` | each operator parses correctly per field; multi-value `_in` splits on comma + strips whitespace; unknown field → ValueError; unknown op → ValueError; type-mismatch (e.g., `size_bytes_gte=abc`) → ValueError |
| `TestParseSort` | default applied when absent; single field; multi-field; direction parsing; tie-breaker always appended; unknown field → ValueError; tie-breaker not duplicated if already in user sort |
| `TestBuildWhereClause` | empty filters → `""` + `[]` params; single field eq; multi-field AND; `_in` produces `IN (?,?,?)`; `_gte`/`_lte` combine for range; param order matches placeholder order |
| `TestBuildOrderByClause` | single field; multi-field; direction emission; produces parameterized-safe field names (never user-controlled values) |
| `TestPropertyBasedSqlInjection` | Hypothesis property test: random valid filter combinations produce only `?` placeholders, never literal values in the SQL string |

### 5.2 `tests/api/test_games_router.py` (~30 tests)

| Class | Tests |
|---|---|
| `TestGamesEmptyDb` | empty table → 200 with `{games: [], meta: {total: 0, has_more: false, ...}}` |
| `TestGamesHappyPath` | single row; multi-row; envelope shape; field set per game; meta fields all present |
| `TestGamesPagination` | default 50; explicit limit; offset progression; limit > 500 → 400; negative offset → 400; `has_more` correct at last page |
| `TestGamesFilterPlatform` | `=steam` returns only steam; `_in=steam,epic` returns both; unknown platform value → empty result (not 400 — value is technically valid wire format) |
| `TestGamesFilterStatus` | `=not_downloaded`; `_in=not_downloaded,pending_update` |
| `TestGamesFilterOwned` | `=1`; `=0`; rows owned and not-owned |
| `TestGamesFilterSizeBytes` | `=` exact match; `_gte` lower-bound; `_lte` upper-bound; `_gte + _lte` range |
| `TestGamesFilterTimeRange` | `last_prefilled_at_gte`; `last_prefilled_at_lte`; `last_validated_at_gte`; `last_validated_at_lte` |
| `TestGamesSortBasic` | default (`title:asc`); explicit `title:desc`; `size_bytes:desc`; tie-breaker on `id:asc` is in `applied_sort` |
| `TestGamesSortMultiField` | `sort=last_prefilled_at:desc,title:asc`; tie-breaker still appended last; pagination stability across pages with tied values |
| `TestGamesAppliedEcho` | `applied_filters` reflects exact parse (with `op` keys); `applied_sort` reflects exact parse including tie-breaker; absent filter not in `applied_filters` |
| `TestGamesUnknownFields` | unknown filter field → 400; unknown sort field → 400; unknown operator → 400 |
| `TestGamesAuth` | unauth → 401; valid token → 200 |
| `TestGamesPoolFailure` | `PoolError` → 503 with structured log + correlation_id (BL6 pattern) |
| `TestGamesMetadataParse` | well-formed JSON in metadata column → parsed object; malformed JSON → null + log `api.games.metadata_parse_failed`; null column → null |
| `TestGamesLastErrorTruncation` | null → null; 199 chars → unchanged; 201 chars → 200 chars; 5000 chars → 200 chars |

### 5.3 Test fixtures

Reuse `tests/api/conftest.py`:
- `unit_app` with `dependency_overrides[get_pool_dep]`
- `client` (httpx.AsyncClient via ASGITransport)
- `populated_pool` (seeded 5 games per BL4 conftest)

Add fixture for BL7-specific game-fixture generators that can produce N-row populated pools for pagination tests.

---

## 6. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Filter injection via crafted query params | Low | Strict allow-list parsed via Pydantic Query() + explicit field/op validation; SQL built parametrically (no string interpolation of values); Hypothesis property test pins this |
| `metadata` column contains malformed JSON | Medium | Catch `json.JSONDecodeError` per row, return `null` for that game's metadata, log `api.games.metadata_parse_failed` with `game_id`; verified by `TestGamesMetadataParse::test_malformed_json_returns_null` |
| `total` COUNT becomes slow on huge tables | Low | At 5K-50K rows, COUNT(*) is sub-millisecond. Future: cache `total` for hot filter combinations if it ever matters (deferred) |
| Pagination drift under concurrent F3 library-sync writes | Low | Acceptable — single-user orchestrator, F3 runs at most daily; tie-breaker on `id:asc` prevents row skipping/dup on tied values |
| `_query_helpers.py` becomes a dumping ground | Medium | Strict scope: parser + validator + SQL-builder only; no domain logic. If `/jobs` needs different filter semantics, it gets its own helper. Code review enforces. |
| `applied_filters/applied_sort` echo schema locks API contract too early | Low-Medium | Per-field `FilterCriterion` model with `extra="forbid"` and explicit `eq/in/gte/lte/gt/lt/ne` keys keeps the contract narrow and self-documenting. Adding a new operator is an additive schema change (new optional field on `FilterCriterion`). |
| Future migration adds a column to `games` and the response model isn't updated | Medium | `extra="forbid"` on `GameResponse` causes Pydantic to raise during model construction in development → caught in CI |
| Future migration adds a new `status` enum value | Medium | `Literal[...]` on `status` field will reject the new value. Mitigation: when adding a status, the response model `Literal` MUST be updated in the same PR. Document this in the migration ADR going forward. |

---

## 7. Documentation deltas

- **CHANGELOG.md:** add to `[Unreleased]` → `### Added` under BL7 heading
- **FEATURES.md:** new Feature 7 entry (BL7 — `GET /api/v1/games`)
- **Security audit:** `docs/security-audits/bl7-f9-games-readonly-security-audit.md`
- **ADR:** none — this spec + CHANGELOG entry constitute the design record. ADR is reserved for cross-cutting decisions; this is a feature on existing substrate.
- **README:** if endpoint list exists, append `/api/v1/games`

---

## 8. Cross-references

- **Spec consumer:** `docs/phase-1/data-model.md` (games table schema + invariants)
- **API substrate:** ADR-0012 + UAT-3 addendum (middleware stack, error patterns, response idiom)
- **Pool API:** ADR-0011 (`read_all` semantics)
- **Conventions parent:** `docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md` (envelope + error semantics this BL inherits and extends)
- **Threat model:** TM-001 (auth — middleware), TM-005 (SQL injection — parametric build), TM-012 (log redaction — ID3 redactor)
- **Bible:** §8 (observability), §10.5 (Depends pattern)

---

## 9. Open follow-ups (deferred, not blocking)

- **Title search** — defer to BL-future-search; needs FTS5 or trigram support; Game_shelf can client-side filter 50 rows trivially in the interim
- **Cursor-based pagination mode** — additive migration if/when an endpoint genuinely needs concurrent-write stability (likely `/jobs` if retention grows to millions of rows)
- **`total`-cache for hot filter combos** — only if profiling shows COUNT(*) latency matters
- **Add `idx_games_size_bytes` index** — only if `?sort=size_bytes:desc` shows up as slow in production
- **Per-game endpoint `GET /api/v1/games/{id}`** — currently no need; Game_shelf reads the list and indexes client-side
