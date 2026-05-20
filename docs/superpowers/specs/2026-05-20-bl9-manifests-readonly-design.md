# BL9-F9 — `GET /api/v1/manifests` (read-only, paginated) — Design Spec

**Date:** 2026-05-20
**Phase:** 2 (Construction), Milestone B, Build Loop 9
**Feature:** F9 partial — third paginated F9 read endpoint
**Branch:** `feat/bl9-manifests-readonly`
**Depends on:** BL5 (FastAPI skeleton), BL6 (envelope conventions), BL7 (`_query_helpers.py`), UAT-4 (helpers hardening), BL8 (jobs validation)
**Extends:** `_query_helpers.py` with one new primitive (`parse_includes` + `IncludeAllowList`) for opt-in FK expansion (the `?include=game` convention).

---

## 1. Goal

Ship the third paginated F9 read endpoint and **introduce the `?include=` opt-in field expansion convention**.

Game_shelf's primary use cases:
- **Manifest history per game:** `?game_id=42&sort=fetched_at:desc&include=game`
- **"Manifest list" view:** default response with `?include=game` to render titles inline
- **Storage-pressure operator query:** `?total_bytes_gte=50000000000&sort=total_bytes:desc&include=game`

Operator CLI:
- "Newest manifests" — default `?sort=fetched_at:desc`
- "Manifests for these games" — `?game_id_in=42,43,44`
- "Big chunk-count manifests" — `?chunk_count_gte=10000`

The previous two paginated F9 endpoints (BL7 games, BL8 jobs) shipped with **zero `_query_helpers.py` changes**. BL9 adds ONE new primitive (`parse_includes`) for the FK-expansion case — a thin, documented extension to the shared convention library.

---

## 2. Locked decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| **D1** | `raw` BLOB column | **Excluded from response** | JSON can't carry binary; base64 would double size; operators don't read zstd-compressed manifests through the API. Out-of-band diagnostic endpoint (`GET /manifests/{id}/raw` returning `application/octet-stream`) is a separate future feature if needed. |
| **D2** | Default sort | **`fetched_at:desc`** | Matches the index `idx_manifests_game_fetched (game_id, fetched_at DESC)`. Semantically "most recently fetched first" — what operators and Game_shelf want. `fetched_at` is NOT NULL (no null-handling trap). Server appends `id:asc` tie-breaker per UAT-4 S2-B. |
| **D3** | `version` filter | **`eq` + `_in`** | String enum-like field; multi-value `_in` enables "show me these specific versions". No LIKE/partial-match (not in helper conventions; would require new operator). |
| **D4** | `?include=game` semantics | **Always-present `game` field, `null` when not requested** | Cleaner client code (one Pydantic shape handles both cases); clearer OpenAPI schema; `extra="forbid"` doesn't fight conditional fields. |
| **D5** | `IncludeAllowList` + `parse_includes` | **NEW primitive in `_query_helpers.py`** | Per-endpoint declaration of permitted expansion keys; comma-separated parse; identifier-validated keys; `include` added to `_RESERVED_PARAM_NAMES`. ~30 LoC. |
| **D6** | `game` summary shape | **3 fields: `title`, `platform`, `app_id`** | Minimal viable for Game_shelf rendering ("Cyberpunk 2077", "epic", "cyberpunk"). Full game details available via `/api/v1/games?id_in=...`. |
| **D7** | JOIN semantics | **`LEFT JOIN games`** (defensive); allow-list scoped to manifests fields only | `manifests.game_id` is NOT NULL FK + ON DELETE CASCADE — game row can't be missing. LEFT JOIN documents intent + survives future FK relaxation. No `game.title` filtering or sorting. |
| **D8** | `applied_includes` echo | **list of strings in `meta`** | Mirrors `applied_filters` and `applied_sort` echo patterns. Empty list when no includes. |
| (D9-D20 inherited) | All BL7+UAT-4+BL8 conventions | — | offset pagination, applied_filters compact dict, identifier validation, _in cap, INT64 ranges, timestamp validator, defensive re-checks in `build_*_clause`, bearer required, PoolError → 503, `extra="forbid"`. |

---

## 3. Wire format

### 3.1 Request

```
GET /api/v1/manifests?<query-params>
Authorization: Bearer <token>
```

**Pagination:** `limit` (default 50, max 500), `offset` (default 0). Inherited.

**Per-endpoint filter allow-list:**

| Field | `=` | `_in` | `_gte` | `_lte` | Value type / Format |
|---|:-:|:-:|:-:|:-:|---|
| `game_id` | ✓ | ✓ | | | int |
| `version` | ✓ | ✓ | | | string |
| `fetched_at` | | | ✓ | ✓ | ISO 8601 timestamp (typed-string validator) |
| `chunk_count` | | | ✓ | ✓ | int (signed 64-bit) |
| `total_bytes` | | | ✓ | ✓ | int (signed 64-bit) |

**Sortable fields:** `id`, `game_id`, `version`, `fetched_at`, `chunk_count`, `total_bytes`.

**Default sort:** `fetched_at:desc`. Server-appended tie-breaker `id:asc`.

**Include allow-list:** `game` (the only opt-in expansion in BL9).

### 3.2 Response — 200 OK

```json
{
  "manifests": [
    {
      "id": 17,
      "game_id": 42,
      "version": "++Fortnite+Release-30.20",
      "fetched_at": "2026-05-20T13:00:00Z",
      "chunk_count": 1820,
      "total_bytes": 80000000000,
      "game": {
        "title": "Fortnite",
        "platform": "epic",
        "app_id": "fortnite"
      }
    }
  ],
  "meta": {
    "total": 487,
    "limit": 50,
    "offset": 0,
    "has_more": true,
    "applied_filters": {"game_id": {"in": [42, 43]}},
    "applied_sort": [
      {"field": "fetched_at", "direction": "desc"},
      {"field": "id", "direction": "asc"}
    ],
    "applied_includes": ["game"]
  }
}
```

**Per-manifest fields** (6 + 1 optional):
- `id`, `game_id`, `version`, `fetched_at`, `chunk_count`, `total_bytes`
- `game`: `{title, platform, app_id}` object when `?include=game` was requested; `null` otherwise (always present in schema)

**`meta` fields:**
| Field | Type | Notes |
|---|---|---|
| `total` | int | Total rows matching filters (before pagination) |
| `limit` | int | Echo of resolved limit |
| `offset` | int | Echo of resolved offset |
| `has_more` | bool | `offset + len(manifests) < total` |
| `applied_filters` | object | Compact `{field: {op: value}}` per UAT-4 S2-A |
| `applied_sort` | array | List of `{field, direction}` including server-appended tie-breaker |
| `applied_includes` | array | List of include keys actually applied (deduped); empty list when no includes |

### 3.3 Error responses

| Status | Body | When |
|---|---|---|
| 400 | `{"detail": "unknown filter field: foo"}` | Query param outside allow-list |
| 400 | `{"detail": "operator 'gte' not allowed for field 'version'"}` | Operator not in per-field allow-list |
| 400 | `{"detail": "include keys not allowed: ['games']"}` | Unknown include key |
| 400 | `{"detail": "invalid value for chunk_count_gte: 'abc' (...)"}` | Value parse failure |
| 400 | `{"detail": "limit must be <= 500, got X"}` | Pagination overflow |
| 401 | (handled by `BearerAuthMiddleware`) | Missing/invalid bearer |
| 503 | `{"detail": "database unavailable"}` | `PoolError` caught at router |

### 3.4 Examples

```
# Game_shelf default manifest panel (default sort: fetched_at:desc; latest first)
GET /api/v1/manifests?include=game&limit=20

# Manifest history for one game
GET /api/v1/manifests?game_id=42&sort=fetched_at:desc&include=game

# Operator: "what manifests are over 50 GB?"
GET /api/v1/manifests?total_bytes_gte=50000000000&sort=total_bytes:desc&include=game

# Operator: "manifest activity in the last 24h"
GET /api/v1/manifests?fetched_at_gte=2026-05-19T00:00:00Z&include=game

# Multi-game query (Game_shelf "manifest history for these games" view)
GET /api/v1/manifests?game_id_in=42,43,44&include=game
```

---

## 4. Architecture

### 4.1 File layout

```
src/orchestrator/api/_query_helpers.py            +~30 LoC (NEW: IncludeAllowList + parse_includes)
src/orchestrator/api/routers/manifests.py         ~250 LoC (new)
src/orchestrator/api/main.py                      +2 lines (import + include_router)
tests/api/conftest.py                             +1 fixture (manifests_pool_seeded)
tests/api/test_query_helpers.py                   +~5 tests (parse_includes coverage)
tests/api/test_manifests_router.py                ~480 LoC, ~30 tests
docs/security-audits/bl9-f9-manifests-readonly-security-audit.md   (audit doc)
```

### 4.2 New helpers module additions

```python
# In _query_helpers.py

@dataclass(frozen=True)
class IncludeAllowList:
    """Per-endpoint declaration of permitted ?include= expansion keys."""

    keys: frozenset[str]

    def __init__(self, keys: set[str] | frozenset[str]) -> None:
        for k in keys:
            _validate_identifier(k, kind="include key")
        object.__setattr__(self, "keys", frozenset(keys))


def parse_includes(
    params: QueryParams,
    *,
    allow_list: IncludeAllowList,
) -> set[str]:
    """Parse ?include= query param into a deduplicated set.

    Empty/absent → empty set. Unknown keys → QueryParamError.
    Comma-separated; per-key whitespace stripped.
    """
    raw = params.get("include")
    if not raw:
        return set()
    requested = {k.strip() for k in raw.split(",") if k.strip()}
    unknown = requested - allow_list.keys
    if unknown:
        raise QueryParamError(
            f"include keys not allowed: {sorted(unknown)}"
        )
    return requested
```

Add `"include"` to module constant `_RESERVED_PARAM_NAMES` (currently `{"limit", "offset", "sort"}`) so future endpoint authors can't declare a filter field named `include`.

### 4.3 Router (`routers/manifests.py`)

Key shape:

```python
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

DEFAULT_SORT = (_SortField(field="fetched_at", direction="desc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

MANIFESTS_FILTER_ALLOW_LIST = FilterAllowList({
    "game_id":      FilterFieldSpec(ops={"eq", "in"}, value_type=int),
    "version":      FilterFieldSpec(ops={"eq", "in"}, value_type=str),
    "fetched_at":   FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
    "chunk_count":  FilterFieldSpec(ops={"gte", "lte"}, value_type=int),
    "total_bytes":  FilterFieldSpec(ops={"gte", "lte"}, value_type=int),
})

MANIFESTS_SORT_ALLOW_LIST = SortAllowList(
    fields={"id", "game_id", "version", "fetched_at", "chunk_count", "total_bytes"}
)

MANIFESTS_INCLUDE_ALLOW_LIST = IncludeAllowList(keys={"game"})


class GameSummary(BaseModel):
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
    game: GameSummary | None   # populated iff "?include=game"


class ManifestsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, dict[str, Any]]
    applied_sort: list[SortFieldResponse]
    applied_includes: list[str]
```

The handler:
1. `parse_pagination` + `parse_filters` + `parse_sort` + `parse_includes` (catch `QueryParamError` → 400)
2. `build_where_clause` + `build_order_by_clause`
3. If `"game" in includes`: SELECT joins `LEFT JOIN games g ON m.game_id = g.id` and adds `g.title AS game_title, g.platform AS game_platform, g.app_id AS game_app_id`
4. Two-query SQL pattern (COUNT + rows)
5. Build response: per-row, populate `game` field from the joined columns if include requested
6. `applied_includes = sorted(includes)` for stable echo

### 4.4 SQL strategy

**Base SELECT (no include):**
```sql
SELECT m.id, m.game_id, m.version, m.fetched_at, m.chunk_count, m.total_bytes
FROM manifests m
<where_sql>
<order_by_sql>
LIMIT ? OFFSET ?
```

**With `?include=game`:**
```sql
SELECT m.id, m.game_id, m.version, m.fetched_at, m.chunk_count, m.total_bytes,
       g.title AS game_title, g.platform AS game_platform, g.app_id AS game_app_id
FROM manifests m
LEFT JOIN games g ON m.game_id = g.id
<where_sql>
<order_by_sql>
LIMIT ? OFFSET ?
```

COUNT query identical in both modes (game JOIN doesn't change row count since `manifests.game_id` is NOT NULL FK):
```sql
SELECT COUNT(*) AS total FROM manifests m <where_sql>
```

**Index utilization:**

| Filter / Sort | Index used | Expected perf |
|---|---|---|
| `?game_id=X` + default sort | `idx_manifests_game_fetched` (covering for both filter + sort) | fast |
| `?game_id_in=A,B,C` | `idx_manifests_game_fetched` (range scan) | fast |
| `?sort=fetched_at:desc` (no game_id filter) | partial use of `idx_manifests_game_fetched` (orders by composite) | acceptable; full sort happens for unfiltered queries |
| `?sort=total_bytes:desc` | full table scan + sort | acceptable at expected scale (thousands of rows); add `idx_manifests_total_bytes` only if profiling shows a hot path |
| `?fetched_at_gte=...` | partial use of index | acceptable |

### 4.5 Wiring in `main.py`

```python
from orchestrator.api.routers.manifests import router as manifests_router
# ...
app.include_router(health_router)
app.include_router(platforms_router)
app.include_router(games_router)
app.include_router(jobs_router)
app.include_router(manifests_router)  # BL9
```

---

## 5. Test plan

Target: ≥95% branch coverage on `routers/manifests.py` and on the new `parse_includes` helpers. ~30 router tests + ~5 helper tests.

### 5.1 `tests/api/conftest.py` — new fixture `manifests_pool_seeded`

Seeds ~30 manifests across the 5 games already in `populated_pool`:
- Multiple manifests per game (history)
- Mix of `version` formats (Steam-style numeric, Epic-style dotted)
- `chunk_count` and `total_bytes` spread across realistic ranges (1 → 50000 chunks; 1 MB → 100 GB)
- `fetched_at` timestamps spread across past month
- `raw` BLOB seeded with a small zstd-ish payload (not actually parsed — just non-null)

### 5.2 `tests/api/test_manifests_router.py` — ~30 tests across 10 classes

| Class | Tests |
|---|---|
| `TestManifestsEmptyDb` | empty → 200 + empty array + total=0 |
| `TestManifestsHappyPath` | seeded returned; 7-field set (including `game: null`); envelope shape |
| `TestManifestsPagination` | default 50; explicit limit; offset progression; limit > 500 → 400 |
| `TestManifestsFilters` | each allow-listed field × operator: game_id (eq, _in); version (eq, _in); fetched_at range; chunk_count range; total_bytes range |
| `TestManifestsSort` | default `fetched_at:desc`; tie-breaker `id:asc` appended; explicit sort by chunk_count, total_bytes; user explicit `id` sort dedupes tie-breaker |
| `TestManifestsAppliedEcho` | applied_filters compact dict; applied_sort with tie-breaker; **applied_includes** empty list when no `?include=`; populated when requested |
| `TestManifestsIncludeGame` | **NEW** — no include = `game: null`; `?include=game` = `game: {...}` populated; `?include=games` (unknown) → 400; duplicate `?include=game,game` deduped; empty `?include=` = no expansion; verify `game.title`/`platform`/`app_id` correct vs the seeded game row |
| `TestManifestsErrorPaths` | unknown filter field → 400; unknown op → 400; unknown sort field → 400; invalid timestamp value → 400 |
| `TestManifestsAuth` | unauth → 401 (smoke) |
| `TestManifestsPoolFailure` | `PoolError` → 503 with structured log |

### 5.3 `tests/api/test_query_helpers.py` — `parse_includes` coverage (~5 tests)

| Test | Coverage |
|---|---|
| `test_absent_returns_empty_set` | no `?include=` → `set()` |
| `test_empty_string_returns_empty_set` | `?include=` → `set()` |
| `test_single_value` | `?include=game` → `{"game"}` |
| `test_multi_value_deduped` | `?include=game,game,game` → `{"game"}` |
| `test_unknown_key_raises` | `?include=games` (typo) → `QueryParamError` |

Also add to `IncludeAllowList` construction: `test_invalid_identifier_rejected` (e.g., `keys={"1=1"}` → `ValueError`).

---

## 6. Risk register

| Risk | Mitigation |
|---|---|
| `LEFT JOIN games` makes COUNT differ from SELECT if game row missing | Schema invariant: `manifests.game_id` is NOT NULL FK + `ON DELETE CASCADE` — game row CANNOT be missing while a manifest exists. `LEFT JOIN` is defensive (survives future FK relaxation); semantically `INNER JOIN` would also work. |
| Field-name collision: `manifests.id` vs `games.id` | SELECT explicitly aliases (`m.id`, `g.title AS game_title`, etc.). Allow-list field names reference `manifests` columns only; no `game.*` filter or sort. |
| `?include=game` doubles per-row wire size | At default `limit=50`, ~80 bytes/row × 50 = ~4 KB extra. Acceptable. Bounded by `limit ≤ 500`. |
| `applied_includes` echo order non-deterministic across runs | `sorted(includes)` before serializing → stable order for test assertions and client caching. |
| `parse_includes` collides with future filter field named `include` | `_RESERVED_PARAM_NAMES` adds `"include"`; `_validate_identifier` rejects it in allow-list construction (raises `ValueError` at endpoint declaration time, caught by CI). |
| Future endpoint wants 2+ include keys | Already supported (comma-separated parse). |
| Future endpoint wants `?include=game` to be default-on | Out of scope for BL9. If Game_shelf reports opt-in friction, additive default-includes set can be added later. |
| `raw` BLOB column accidentally selected by future code | `_MANIFEST_COLUMNS` constant explicitly lists 6 manifest columns; `raw` not in the list. Schema change would require code change. |
| Future migration adds a manifest column | `extra="forbid"` on `ManifestResponse` raises during construction → caught in CI |

---

## 7. Documentation deltas

- **CHANGELOG.md** — add to `[Unreleased]` → `### Added` (BL9 entry)
- **FEATURES.md** — new Feature 9 entry
- **Security audit** — `docs/security-audits/bl9-f9-manifests-readonly-security-audit.md`
- **ADR** — none. This spec + CHANGELOG entry are the design record.

---

## 8. Cross-references

- **BL7 spec:** `docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md` (template ancestor)
- **BL8 spec:** `docs/superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md` (immediate predecessor)
- **UAT-4 closure:** `docs/security-audits/uat-4-remediation-security-audit.md` (12 fixes BL9 inherits)
- **Data model:** `docs/phase-1/data-model.md` (manifests table schema + invariants)
- **API substrate:** ADR-0012 + UAT-3 addendum

---

## 9. Open follow-ups (deferred, not blocking)

- **Per-manifest endpoint `GET /api/v1/manifests/{id}`** — clients can read the list; if a real need surfaces, additive
- **`GET /api/v1/manifests/{id}/raw`** — `application/octet-stream` download of the zstd-compressed manifest blob; operator diagnostic only; deferred until a real need surfaces
- **`?include=` default-on opt-in** — surface Game_shelf preference if/when implemented
- **`idx_manifests_total_bytes` / `idx_manifests_chunk_count`** — only if profiling shows hot paths
- **Cursor-based pagination mode** — additive if/when retention grows to millions of rows
- **JOIN `games.status`, `games.size_bytes` into GameSummary** — minimal-by-design now; expand when Game_shelf UX explicitly asks
