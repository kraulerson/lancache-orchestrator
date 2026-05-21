# UAT-5 Agent 3 — Cross-Feature Regression Audit

**Scope:** BL6 (platforms) / BL7 (games) / BL8 (jobs) / BL9 (manifests) — cross-router
consistency, shared module drift, middleware ordering, DI, OpenAPI schema, fixture
interactions, migration FKs, log structure.
**Date:** 2026-05-20
**Working dir:** /Users/karl/Documents/Claude Projects/lancache_orchestrator

## Executive summary

The four F9 read endpoints are, overall, well-aligned. UAT-3 and UAT-4 left strong
convention scaffolding (`extra="forbid"` everywhere, plain-dict `applied_filters`,
shared `_query_helpers.py`, CORS-outermost middleware), and BL8/BL9 inherited them
faithfully. The audit found:

- **3 real cross-router inconsistencies** worth filing
- **2 dead-code / stale-comment items** worth a follow-up
- **1 OpenAPI fragility** that is dormant but will bite the next change
- **0 security regressions, 0 schema bugs, 0 fixture leakage**

Severity scale: SEV-2 = visible inconsistency or breaks a documented convention;
SEV-3 = correctness-adjacent or fragile; SEV-4 = cosmetic / documentation drift.

---

## SEV-2 Findings

### F1. `/api/v1/platforms` silently ignores unknown query params; the other 3 endpoints 400 on them

**Files**
- `src/orchestrator/api/routers/platforms.py:59-87` — handler signature is
  `async def list_platforms(pool: Pool = Depends(...))` — **no `request: Request` parameter**.
  The handler never sees the query string at all.
- `src/orchestrator/api/routers/games.py:168-186` — explicitly parses + validates
  query params; raises `QueryParamError` → 400 on any unknown field.
- `src/orchestrator/api/routers/jobs.py:138-156` — same.
- `src/orchestrator/api/routers/manifests.py:142-161` — same.

**Expected vs actual**
A caller doing `GET /api/v1/platforms?foo=bar` gets `200 OK` with the full platform
list. The same caller doing `GET /api/v1/games?foo=bar` gets
`400 {"detail": "unknown filter field: foo"}`. The strict posture documented in
UAT-4 `_query_helpers.py` ("unknown filter field" is a hard 400) does not apply to
platforms.

**Impact**
- Operator UX: an operator who typos `?status=ok` against `/platforms` (intending
  `/games?status=ok`) gets back ALL platforms with no signal that the filter was
  ignored. The same typo against `/games` returns 400, surfacing the mistake.
- Convention drift: undermines the "strict allow-list" posture that BL7+ locked
  in. Future endpoints may copy whichever pattern they happened to read first.
- Security-adjacent: not a vulnerability, but a defense-in-depth principle says
  every endpoint should reject unrecognized input.

**Suggested action**
Decide a convention and document it. Two reasonable paths:
1. **Strict (preferred):** add `request: Request` to `list_platforms` and reject any
   query params (since platforms supports none). Echo nothing in the body since
   there's no meta envelope. Matches the BL7+ posture.
2. **Document & accept:** add an ADR-level note that fixed-shape endpoints
   (platforms is always 2 rows; no pagination, no filters) silently ignore query
   params, and other fixed-shape endpoints (future) should too. Less work, but
   leaves operators without the typo signal.

Recommend path 1: cheap to implement (~10 lines), matches the convention.

---

### F2. Wrapped-envelope shape diverges between platforms and the three paginated endpoints

**Files**
- `src/orchestrator/api/routers/platforms.py:34-37` — `PlatformListResponse` has
  exactly one key: `platforms`. No `meta` block.
- `src/orchestrator/api/routers/games.py:138-141` — `{games, meta}`.
- `src/orchestrator/api/routers/jobs.py:106-109` — `{jobs, meta}`.
- `src/orchestrator/api/routers/manifests.py:111-114` — `{manifests, meta}`.

**Expected vs actual**
This is partly defensible (platforms is fixed 2-row; pagination/filter/sort meta
would be vacuous). But the BL6→BL7 convention bump went the other way: BL6 locked
"wrapped envelope" as the convention, and BL7+ extended it to "wrapped envelope +
meta". A client that wrote a generic "unwrap" helper for the three paginated
endpoints (`body[entity_name]`) works on platforms too — fine. But a client that
reads `body["meta"]` blindly fails on platforms.

**Impact**
- Client-library complexity: a generic F9 client cannot treat all 4 endpoints
  uniformly.
- The platforms doctring at `src/orchestrator/api/routers/platforms.py:51-57` says
  "Always returns exactly two rows" — so adding a no-op `meta: {total: 2, limit:
  None, offset: None, ...}` would be misleading.

**Suggested action**
Document the divergence explicitly in the F9 design doc (or ADR) and call out that
**fixed-shape resource endpoints** intentionally omit `meta`. This is a docs fix,
not a code fix. Adding meta to platforms would be worse than the inconsistency.

---

### F3. `applied_filters` echoes only user-supplied filters, not default-applied state — convention undocumented

**Files**
- `src/orchestrator/api/routers/games.py:268-270`
- `src/orchestrator/api/routers/jobs.py:224-226`
- `src/orchestrator/api/routers/manifests.py:228-230`

**Expected vs actual**
All three paginated endpoints echo only filters the user typed; defaults (which
currently are empty — none of the endpoints apply implicit filters) are not
echoed. This is the correct current behavior. But neither the spec nor the
runtime model documents the rule. If a future endpoint adds an implicit filter
(e.g., `/jobs` defaults to `state_in=queued,running` for an "active jobs" view),
will the echo include it or not?

**Impact**
- Latent bug-bait: the next person adding a default filter will have to make a
  judgment call. The convention should be locked.

**Suggested action**
Add a one-line comment to `_query_helpers.py` (near `parse_filters`) and update
the F9 spec to state: **applied_filters echoes user-typed filters only**. If a
default filter is in effect, surface it via a separate `default_filters` field in
meta. This is a one-paragraph spec edit; no code change needed today.

---

## SEV-3 Findings

### F4. `SortFieldResponse` is defined in three routers; OpenAPI silently dedupes — fragile

**Files**
- `src/orchestrator/api/routers/games.py:118-121`
- `src/orchestrator/api/routers/jobs.py:90-93`
- `src/orchestrator/api/routers/manifests.py:93-96`

**Verified observation** (from running `create_app().openapi()`):
The three classes happen to be structurally identical (`field: str`,
`direction: Literal["asc","desc"]`, `extra="forbid"`). FastAPI's OpenAPI generator
collapses them into a single `SortFieldResponse` component schema. All three
`*Meta.applied_sort` correctly `$ref` to it.

**Why it's fragile**
The moment any one router adds a field to its `SortFieldResponse` (e.g., a future
"applied tie-breaker?" boolean), the generator will create `SortFieldResponse2` or
similar, and clients keyed by name will break silently.

**Suggested action**
Move the model to `_query_helpers.py` (or a new `_models.py`) and import it in all
three routers. Single source of truth. ~5 lines moved; no behavior change. This
also reduces line count in each router.

---

### F5. Dead-but-comment-claims-alive `FilterCriterion` model in games.py

**Files**
- `src/orchestrator/api/routers/games.py:104-115` — defines `FilterCriterion`.
- `src/orchestrator/api/routers/games.py:130-133` — comment claims it is "kept
  above only so OpenAPI schema generation documents the valid `op` keys".

**Verified observation** (from inspecting openapi schema):
`FilterCriterion` is **NOT** referenced in the emitted OpenAPI schema. Confirmed
by `'FilterCriterion' in schema['components']['schemas']` returning `False`.
`GamesMeta.applied_filters` is typed as `dict[str, dict[str, Any]]`, which the
generator renders as a plain `additionalProperties: {additionalProperties: true}`.
No `$ref` to `FilterCriterion` exists anywhere in the schema.

The comment is wrong; the model is dead code.

**Impact**
Cosmetic + misleading future readers. The comment promises documentation value
that doesn't exist.

**Suggested action**
Either (a) actually wire it in by changing `applied_filters` type to
`dict[str, FilterCriterion]` (which restores the all-7-op-keys-with-6-nulls bug
UAT-4 fixed — DON'T do this), or (b) delete `FilterCriterion` and update the
comment. Choose (b). The applied_filters runtime shape is documented in the spec
and in a test (`test_applied_filters_compact_dict_shape`); that's the SoT.

---

### F6. Constant naming inconsistency: error-truncation length

**Files**
- `src/orchestrator/api/routers/platforms.py:19` — `_LAST_ERROR_TRUNCATE = 200`
- `src/orchestrator/api/routers/games.py:37` — `LAST_ERROR_TRUNCATE = 200`
- `src/orchestrator/api/routers/jobs.py:35` — `ERROR_TRUNCATE = 200`
- `src/orchestrator/api/routers/manifests.py` — N/A (no error field)

Three different names for the same constant value with the same semantic meaning.

**Impact**
Cosmetic. But these are the kind of micro-inconsistencies that compound when
future endpoints copy whichever they happened to see.

**Suggested action**
Promote to `_query_helpers.py` (or `dependencies.py`) as `ERROR_TRUNCATE_LEN = 200`
or similar. Single import in each router. ~3-line change per file.

---

## SEV-4 Findings (cosmetic / doc drift)

### F7. `manifests_pool_seeded` fixture docstring says "~21 manifests"; actual count is 24

**File:** `tests/api/conftest.py:233-242` (docstring claims 21) vs `:247-269`
(seeds 21 NEW rows) — but the fixture extends `populated_pool` which ALREADY
seeds 3 manifests at `tests/db/conftest.py:117-123` (game_ids 1-3, version='1.0').
Total: 24.

**Impact**
None functional — tests use `>= 20` style assertions, and the version filter
tests pick versions that don't collide with the inherited '1.0'. But the
docstring is wrong and a future test author reading it might choose colliding
test data.

**Suggested action**
Update the docstring to say "24 manifests (3 inherited from populated_pool +
21 added)" and call out that inherited rows have `version='1.0'`. One-line doc
fix.

### F8. Comment in `main.py:165` lists "Registration order (innermost → outermost):" but is truncated mid-sentence

**File:** `src/orchestrator/api/main.py:165` — comment ends with a colon and a
blank line; the list that should follow is just the four `add_middleware` calls
below it. Reads as incomplete.

**Impact**
None functional. Cosmetic.

**Suggested action**
Either complete the comment with the explicit ordering list, or delete the
truncated colon. Trivial.

---

## What's correctly consistent (the boring good news)

The audit specifically verified the following and found them clean:

1. **Auth posture (all 4 endpoints):** All require bearer; all rely on the central
   `BearerAuthMiddleware` (no per-router auth bypass); none are in
   `AUTH_EXEMPT_PATHS`. Confirmed in `dependencies.py:28-33`.
2. **Error response shapes:** 400 → `{"detail": "..."}`, 401 → `{"detail":
   "unauthorized"}` + WWW-Authenticate header, 503 → `{"detail": "database
   unavailable"}`. Identical wire shapes across all 4 (where applicable —
   platforms has no 400 because no query params).
3. **Pagination behavior (3 paginated endpoints):** Identical `DEFAULT_LIMIT=50`,
   `MAX_LIMIT=500`, identical `parse_pagination` call sites, identical
   `has_more` formula `(offset + len(items) < total)`. Confirmed in games:35-36,
   jobs:33-34, manifests:34-35 + lines 280, 235, 241 respectively.
4. **`extra="forbid"` posture:** Every response model in every router has
   `model_config = ConfigDict(extra="forbid")`. 18/18 models verified. No drift.
5. **Middleware ordering (UAT-3 ADR-0012 D5):** Outermost → innermost is
   CORS → CorrelationId → BodySizeCap → BearerAuth. Verified in `main.py:167-177`.
   Comment at lines 153-166 correctly explains the trade-off (CORS-rejected
   requests lack correlation_id in logs).
6. **DI uniformity:** All four read endpoints declare `pool: Pool =
   Depends(get_pool_dep)`. `unit_app` fixture overrides via
   `app.dependency_overrides[get_pool_dep]` and that override propagates to all
   four routers without per-router config. Verified by grep + reading
   conftest.py:304.
7. **Shared module allow-list consistency:** `jobs.platform` and `games.platform`
   both use `FilterFieldSpec(ops={"eq", "in"}, value_type=str)`. No drift.
   `jobs.state` (jobs table) and `games.status` (games table) are correctly
   different field names (no collision; the schema uses different column names).
8. **Sort allow-list consistency:** Both `jobs` and `games` make `platform`
   filterable but NOT sortable. Consistent posture (platform values are sparse
   2-element enums; sorting by them is rarely useful).
9. **OpenAPI schema integrity:** All 5 paths emitted
   (`/api/v1/{health,platforms,games,jobs,manifests}`). All response models
   present in components.schemas. No `extra=forbid` violations exposed.
   `bearerAuth` security scheme correctly registered via `custom_openapi`
   wrapper at `main.py:182-201`.
10. **Fixture state leakage:** All pool / db_path / populated_pool fixtures are
    pytest function-scoped (default). Each test gets a fresh tmp_path DB.
    Confirmed `tmp_path` is function-scoped by pytest design.
11. **Hard-coded IDs:** `test_games_router.py:50` asserts `total == 5` —
    correct because `populated_pool` seeds exactly 5 games. Other tests either
    use `>=` thresholds or filter by content (title, app_id), avoiding ID
    dependence. No collisions found.
12. **Migration FK paths:** `migrations/0001_initial.sql:35` games.platform ON
    DELETE RESTRICT (can't delete a platform with games); `jobs.game_id` ON
    DELETE SET NULL (jobs.game_id becomes NULL, not orphaned — and the router
    correctly types `game_id: int | None`); `manifests.game_id` ON DELETE
    CASCADE (matches the manifests.py comment at line 192). The manifests
    router's `?include=game` lookup is correct: cascade guarantees no orphan
    game_ids in the manifests table.
13. **Log namespacing:** All four routers emit `api.{platforms|games|jobs|manifests}.read_failed`
    for PoolError. Per-endpoint warnings use parallel structure
    (`metadata_oversized` / `payload_oversized`; `metadata_parse_failed` /
    `payload_parse_failed`). Field name `reason=type(e).__name__` consistent.
    No sensitive data leakage in log lines (raw payload/metadata never logged;
    only size + exception class name).
14. **CORS allow_methods:** `["GET", "POST", "DELETE", "OPTIONS"]` at
    `main.py:174`. Currently the 4 endpoints only use GET; the extra methods are
    forward-looking for write endpoints. Not a bug.

---

## Recommended remediation order

Priority by SEV + cost to fix:

1. **F1 (platforms query param posture)** — SEV-2, ~10 LoC. Locks F9 convention.
2. **F4 (move SortFieldResponse to shared module)** — SEV-3, ~5 LoC moved + 3
   imports. Cheapest fragility fix.
3. **F5 (delete dead FilterCriterion)** — SEV-3, ~15 LoC removed + comment fix.
4. **F6 (unify ERROR_TRUNCATE_LEN constant)** — SEV-3, ~6 LoC across 3 files.
5. **F2 (document envelope-shape divergence)** — SEV-2 but docs-only. Add to
   F9 ADR / spec.
6. **F3 (document applied_filters echo convention)** — SEV-2 but docs-only.
7. **F7, F8** — SEV-4 trivia, do as cleanup pass.

Estimated total: ~40 LoC + 3 short doc updates. None touch the shared
`_query_helpers.py` (which validates the "convention propagation" thesis recorded
in `project_bl8_jobs_complete.md`).

---

## Files audited

- src/orchestrator/api/main.py (228 lines)
- src/orchestrator/api/middleware.py (327 lines)
- src/orchestrator/api/dependencies.py (75 lines)
- src/orchestrator/api/_query_helpers.py (495 lines)
- src/orchestrator/api/routers/platforms.py (88 lines)
- src/orchestrator/api/routers/games.py (286 lines)
- src/orchestrator/api/routers/jobs.py (241 lines)
- src/orchestrator/api/routers/manifests.py (248 lines)
- src/orchestrator/api/routers/health.py (skimmed)
- src/orchestrator/db/migrations/0001_initial.sql (169 lines)
- tests/api/conftest.py (345 lines)
- tests/db/conftest.py (147 lines)
- tests/api/test_platforms_router.py (skimmed)
- tests/api/test_games_router.py (skimmed)
- tests/api/test_jobs_router.py (grepped)
- tests/api/test_manifests_router.py (skimmed)
- Live OpenAPI schema via `create_app().openapi()`
