# UAT-5 Agent 1 — Automated Suite Audit (BL7/BL8/BL9)

**Date:** 2026-05-20
**Agent role:** QA Test Engineer — hunt bugs the existing tests MISS, not bugs the tests already catch.
**Scope:** `games.py` (BL7), `jobs.py` (BL8), `manifests.py` (BL9), shared `_query_helpers.py`, `main.py` (wiring), plus the four test modules.

## Baseline

- `pytest -q --no-header` → **560 passed, 3 deselected in 16.62s**. Suite is clean.

## Method

1. Read all three routers and the shared helpers module line-by-line; enumerated every conditional branch and Pydantic field set.
2. Read the four test modules and mapped covered request URLs to router branches.
3. Identified gaps where (a) branches have no covering test, or (b) edge cases at the parameter / type boundary aren't exercised, with focus on the categories called out in the task brief.
4. No test code was modified or executed beyond the baseline run.

Findings are ranked by severity. References use absolute paths and 1-indexed line numbers.

---

## SEV-2 findings

### S2-A — Bearer auth silently strips non-ASCII bytes; token comparison done on a decoded ASCII string
**Files:**
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/middleware.py:245`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/middleware.py:273`

**Branch / behavior:** the `Authorization` header is decoded with `errors="ignore"`. If a request sends a header containing non-ASCII bytes — e.g. UTF-8 encoded `Bearer foo\xc2\xa0bar` (non-breaking space embedded in the token) — those bytes silently disappear before the `hmac.compare_digest` check. Two distinct token strings can therefore map to the same comparison input.

**Why it matters:** the secret value comes from settings (`expected.encode("utf-8")`), so the *attacker's* candidate token is normalized but the *real* token is not. In practice this is non-exploitable today because the real token is required to be hex/base64 ASCII, but the silent normalization is a latent timing / equality surface that the automated tests never probe. There is no test for:
- 1-byte token
- empty-but-present token (covered)
- 200 KiB token (no upper bound enforced before hmac → unbounded server CPU on attacker-controlled length, very mild DoS)
- header bytes that fail UTF-8 decode (silently dropped, equality result unspecified relative to operator expectations)
- mixed-scheme casing combined with garbage after the scheme (e.g. `BeArEr\x00\x00token` — null bytes preserved through ASCII-with-ignore)

**Severity:** SEV-2 because it touches the auth boundary and the existing test set asserts neither presence nor absence of these behaviors. Recommend explicit auth-input fuzzing tests asserting "anything that isn't an exact byte match of the expected token returns 401".

**Reproducer:** add a test
```python
async def test_token_with_non_ascii_bytes_is_rejected(client, populated_pool):
    r = await client.get(
        "/api/v1/games",
        headers={"Authorization": "Bearer aaaa\xc2\xa0aaaa" + "a" * 24},
    )
    assert r.status_code == 401
```

---

### S2-B — Pydantic `Literal` columns crash to 500 if the DB row holds an unexpected value
**Files:**
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/games.py:81-97` (`platform`, `status`)
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/jobs.py:78-83` (`kind`, `platform`, `state`, `source`)

**Branch / behavior:** each response model declares enum columns as `Literal[...]`. Pydantic raises `ValidationError` if the DB value is outside the allow-list. That error propagates as an unhandled exception out of `list_games` / `list_jobs` — there is no `try/except` around the per-row `GameResponse(...)` / `JobResponse(...)` construction, only around the SQL execution.

**Why it matters:** the DB has CHECK constraints today, but:
1. A future migration may add a new enum value without updating the API model.
2. Direct SQL writes (CLI, test fixtures, ad-hoc support scripts) can insert rows that bypass the CHECK if a migration drops it.
3. The error path here is a full request crash (500), not a structured 503 or row-skip.

**Tests miss:** no test inserts a row with an out-of-Literal value to verify the failure mode. The router has no defensive logging at row-construction time. Compare to `metadata`/`payload` parsing which catches the equivalent malformed-row case and returns `None`.

**Reproducer:** add a row with `INSERT INTO games (..., status='garbage_value', ...)` via raw SQL and call `GET /api/v1/games` → uncaught Pydantic error.

**Recommendation:** wrap per-row response-model construction in a try/except logging the row id and skipping (or returning a sentinel status), matching the metadata-parse robustness pattern.

---

### S2-C — `len()` size cap on metadata/payload crashes if DB driver returns a non-buffer type
**Files:**
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/games.py:214-239`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/jobs.py:180-202`

**Branch / behavior:** the size-cap check runs `len(raw_meta) > _MAX_METADATA_BYTES` *before* `json.loads`. The `try/except` only wraps `json.loads`. If `raw_meta` is ever a `dict` (e.g. a pool driver that auto-decodes JSON columns, or a future migration to JSON1 with adapter), `len()` returns the dict's key count (works, but compares against 65536 bytes meaninglessly) — but `isinstance` check + length semantics get confused. More dangerously, if the pool returns an `int`/`bool`/other non-sized type, `len()` raises `TypeError` which is **not** caught here (the except only covers the inside of the `else` branch).

**Why it matters:** robustness against driver/adapter changes. Today the aiosqlite pool returns `str` or `None`, so the path is safe, but UAT-4 already demonstrated that small driver/adapter changes can trigger unhandled exceptions in this exact area.

**Tests miss:** no test patches the pool to return a non-`str|bytes|None` for `metadata`/`payload`.

**Recommendation:** narrow the typing path: `if not isinstance(raw_meta, (str, bytes, bytearray)):` short-circuit to `metadata = None` with a warn log. Or move the length check inside the same try/except.

---

## SEV-3 findings

### S3-A — `offset > total` not asserted; client paginating past the end gets undocumented behavior
**Files:**
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/games.py:280` (has_more)
- `jobs.py:235`, `manifests.py:241`

**Behavior trace:** with `?limit=50&offset=10000` against a 100-row table:
- `count_row.total = 100`
- `rows = []` (SQL `OFFSET 10000` returns nothing)
- `has_more = (10000 + 0 < 100)` = `False` → correct
- `total = 100`, response has `games: []` with `meta.total: 100`

This is the right behavior; the bug is that **no test asserts it**. A future refactor could regress to `has_more = (offset + limit < total)` (which would be True here, misleading clients into paginating further). The test suite would not catch the regression.

**Severity:** SEV-3 — correctness invariant currently honored but unprotected.

**Recommended test:**
```python
async def test_offset_past_end_returns_empty_with_total(client, games_pool_100):
    r = await client.get(
        "/api/v1/games?limit=50&offset=10000",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    body = r.json()
    assert body["games"] == []
    assert body["meta"]["total"] == 100
    assert body["meta"]["has_more"] is False
    assert body["meta"]["offset"] == 10000
```
(And parallels for `/jobs`, `/manifests`.)

---

### S3-B — `?platform_in=` and `?platform_in=,steam,` accepted; empty values pass through to SQL
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:300-307`

**Branch / behavior:** in `parse_filters`, the `_in` branch does:
```python
raw_values = raw_value.split(",")
if len(raw_values) > MAX_IN_VALUES: ...
values = [_coerce_value(v.strip(), spec.value_type, field_name, op) for v in raw_values]
```

Three untested edge cases:
1. **`?platform_in=`** → `raw_value = ""`, `raw_values = [""]`, `_coerce_value("", str, ...)` returns `""`. SQL becomes `WHERE platform IN (?)` with `[""]`. Echoes `applied_filters: {platform: {in: [""]}}`. No rows match (no row has empty-string platform) but the silent acceptance of an empty value is surprising — `?platform=` (eq) goes through the same path with `eq`, also accepted as `""`.
2. **`?platform_in=,steam,`** → `["", "steam", ""]`, length 3. Generates `WHERE platform IN (?,?,?)` with `["", "steam", ""]`. Matches steam rows. Echoes `applied_filters` with the empty-string sentinels.
3. **`?platform_in=steam,steam,steam`** (dup values) — no dedup; all three placeholders emitted; SQL is correct but wasteful.

**Severity:** SEV-3. Silent acceptance of empty / duplicate `_in` values violates the principle of least surprise; the API contract should either reject empties or strip them. No tests cover any of these.

**Reproducer:**
```python
async def test_in_op_empty_value_rejected(client, games_pool_100):
    r = await client.get("/api/v1/games?platform_in=", headers={...})
    assert r.status_code == 400   # currently passes 200 with empty result
```

---

### S3-C — `_in` boundary at exactly `MAX_IN_VALUES` (100) untested per-endpoint
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:302`

**Branch:** `if len(raw_values) > MAX_IN_VALUES`. The boundary check uses strict `>`, so 100 values is accepted and 101 rejected. Neither boundary is tested at the integration layer for any of the three endpoints. `test_query_helpers.py` doesn't cover this either; only the in-op syntax is tested.

**Severity:** SEV-3 — boundary regression risk. A future change to `>=` (or to `MAX_IN_VALUES` itself) would not be caught.

---

### S3-D — Multi-op filter on same field (`?size_bytes_gte=100&size_bytes_lte=500`) — `applied_filters` echo shape not asserted
**Files:** all three router tests.

**Behavior trace:** `parse_filters` builds `{"size_bytes": {"gte": 100, "lte": 500}}`. The router emits `applied_filters` as a plain dict so the response contains:
```json
{"size_bytes": {"gte": 100, "lte": 500}}
```

The test `test_gte_lte_combined` (games_router L249) and `test_chunk_count_range` (manifests_router L174) verify the *filtered rows* match the range, but neither asserts the **applied_filters echo shape** has both keys present in the same object. A regression where the second op silently overwrites the first (e.g. `result[field_name] = {op: value}` instead of `setdefault`) would still pass these tests because the SQL would still apply both predicates if the loop is restructured the wrong way. Tests should pin the echo shape.

**Severity:** SEV-3 — contract drift risk.

**Recommended assertion:**
```python
r = await client.get("/api/v1/games?size_bytes_gte=100&size_bytes_lte=500&limit=10", ...)
assert r.json()["meta"]["applied_filters"]["size_bytes"] == {"gte": 100, "lte": 500}
```

---

### S3-E — Inverted range (`?size_bytes_gte=500&size_bytes_lte=100`) returns empty silently; no contract decision documented
**File:** logic implicit in `parse_filters` + `build_where_clause`.

**Behavior:** an inverted range produces `WHERE size_bytes >= 500 AND size_bytes <= 100`, which matches zero rows. The 200 response carries `meta.total: 0`, `games: []`, and `applied_filters: {size_bytes: {gte: 500, lte: 100}}`.

This is defensible (silent empty result), but the project has no test pinning it, and the spec doesn't explicitly say whether such queries should 400 or 200-empty. UAT-4 already locked `_in` cap + ISO timestamp; range-inversion is a similar contract surface.

**Severity:** SEV-3 — contract gap.

**Recommendation:** add either (a) a 400 with `"empty range: gte > lte"` in `parse_filters` when both keys present and `gte > lte`, or (b) an explicit test pinning the 200-empty behavior so it can't regress.

---

### S3-F — `?sort=,,,` (all-empty entries) UAT-4 S2-B fix not tested at the integration layer
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:436-437`

**Branch:** `if not user_sort: user_sort = list(default)` is the UAT-4 S2-B regression fix. This is unit-tested implicitly (the default-when-absent case at `test_default_applied_when_absent`) but **not** with a non-empty `sort` param whose entries are all empty. None of the three integration test modules exercise `?sort=,,,` or `?sort=,`.

**Severity:** SEV-3 — regression risk specifically on the bug UAT-4 already had to fix once.

**Recommended test:**
```python
async def test_sort_all_empty_entries_falls_back_to_default(client, games_pool_100):
    r = await client.get("/api/v1/games?sort=,,,&limit=5", headers={...})
    assert r.status_code == 200
    applied = r.json()["meta"]["applied_sort"]
    assert applied == [{"field": "title", "direction": "asc"}, {"field": "id", "direction": "asc"}]
```

---

### S3-G — User sort already containing tie-breaker field with explicit direction — only single-field case tested
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:440-441`

**Branch:** `if not any(s.field == tie_breaker.field for s in user_sort): user_sort.append(tie_breaker)`. Dedup is by field name, ignoring direction.

**Tested:** `test_user_id_sort_dedupes_tie_breaker` (games L314, manifests L224) — single-field user sort `?sort=id:desc`.

**NOT tested:** multi-field user sort with tie-breaker field present in either first or middle position. E.g.:
- `?sort=title:asc,id:asc` — user already has tie-breaker; should dedup
- `?sort=id:desc,title:asc` — tie-breaker first; should dedup (no extra append) but server SQL becomes `ORDER BY id DESC, title ASC` which loses determinism for ties on (id,title) — that's a logical impossibility for id, so fine — but the test should pin the applied_sort echo.
- `?sort=title:asc,id:desc,status:asc` — tie-breaker in middle; should dedup.

**Severity:** SEV-3 — branch under-covered.

---

### S3-H — `?sort=title:asc,title:desc` (same field twice) accepted; SQL becomes `ORDER BY title ASC, title ASC, id ASC` or similar
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:413-431`

**Branch:** the parser doesn't dedup user-supplied sort fields. Two entries for the same field both pass validation and both get appended to `user_sort`. SQL ends up with redundant `ORDER BY title ASC, title DESC, id ASC` — the second clause is dead (the first already fully orders by title), but SQLite still accepts it.

**Severity:** SEV-3 — wasted CPU + counterintuitive behavior. Untested.

**Recommendation:** dedup by field name in `parse_sort`, keeping the first occurrence. Or 400 on duplicates.

---

### S3-I — Multiple same-key filter params (e.g. `?platform=steam&platform=epic`) — first wins silently
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:276-310`

**Branch:** the loop `for key in params:` iterates **all** occurrences (Starlette's QueryParams supports multi-value), and `params[key]` returns the **first** value (Starlette default). The net effect: if the user sends `?platform=steam&platform=epic`, the loop body executes twice with the same key, and on the second iteration `result.setdefault(field_name, {})["eq"] = "steam"` (because `setdefault` already returned the dict, then the assignment overwrites the prior `eq` with — again — `"steam"`). So second occurrence is a no-op; second value is silently ignored.

**Severity:** SEV-3 — surprising. The client thinks they're filtering on both; the API only honors the first. Should either 400 or treat as `_in`. Untested.

**Reproducer:**
```python
r = await client.get("/api/v1/games?platform=steam&platform=epic&limit=500", ...)
# Currently returns only steam rows; client expected steam+epic.
```

---

### S3-J — `BL9 ?include=GAME` (case sensitivity) untested
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:487-494`

**Branch:** `parse_includes` does exact-string `requested - allow_list.keys`. Allow-list is `{"game"}`. `?include=GAME` is rejected with 400 (not in allow-list). This is the correct behavior (consistent with case-sensitive everywhere else in the helpers), but **untested**. A future "case-insensitive includes" misfeature would regress without notice.

**Severity:** SEV-3.

---

### S3-K — `?include=game,,game` (empty interstitial values, dedup) untested
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:490`

**Branch:** `{k.strip() for k in raw.split(",") if k.strip()}` — empty entries are dropped by the `if k.strip()` filter. Tested for trailing/middle whitespace (`test_whitespace_stripped`), tested for `include=` alone (`test_empty_string_returns_empty_set`), tested for plain dedup (`test_multi_value_deduped`). **NOT tested:** mixed empty + valid (`?include=game,,game,,`), which is the exact pattern a buggy client would send.

**Severity:** SEV-3 (minor) — but the same logic is the only line standing between "well-formed" and "looks-like-injection" in the include parser, so it deserves a regression pin.

---

### S3-L — `progress` float filter accepts `inf`, `nan`, negative values; no domain validation
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:239` (`if value_type is float: return float(raw)`)

**Branch:** `float("inf")` → `inf`, `float("nan")` → `nan`, `float("-1.5")` → `-1.5`. All pass coercion. The progress column semantically lives in `[0.0, 1.0]`, but the filter accepts arbitrary floats. SQLite compares NaN as not-equal-to-anything → returns empty (silent zero rows); inf compares correctly → also empty result. Tests don't cover any of these.

**Severity:** SEV-3 — silent empty results vs. a clear 400 is poor UX. Also, `float("nan")` going through `?` placeholder may behave differently across DB drivers and is worth pinning.

**Reproducer:**
```python
r = await client.get("/api/v1/jobs?progress_gte=inf&limit=10", ...)
assert r.status_code == 400  # currently 200 with empty result
```

---

### S3-M — Negative int for unsigned-semantic field (`size_bytes`, `chunk_count`, `total_bytes`) accepted
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:233-238`

**Branch:** integer coercion only enforces INT64 range — there is no per-field "must be >=0" check. `?size_bytes_gte=-1000000` is accepted and returns all rows (matches the implicit "every size_bytes is >= -1M" predicate). Untested.

**Severity:** SEV-3 — same class as S3-L. Not exploitable; just sloppy API contract.

**Recommendation:** add optional `min_value` / `max_value` to `FilterFieldSpec` and enforce per-field bounds at coercion. Or document the API surface as "any int64 accepted, semantics-of-empty-result is the client's problem".

---

### S3-N — `applied_filters` ordering not asserted in any test
**Files:** all three router tests.

**Behavior trace:** the `applied_filters` dict is built from `parse_filters` output, whose order is the iteration order of `request.query_params`. JSONResponse serializes dict in insertion order. Tests only assert membership of specific keys, never the full dict equality with ordering, and never that the order is stable across calls.

**Severity:** SEV-3 — implicit contract that clients may rely on; not pinned.

---

### S3-O — `manifests.py` game-expansion path: empty page with `?include=game` still echoes `applied_includes: ["game"]`
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/manifests.py:194` (guard: `if "game" in includes and rows:`)

**Behavior trace:** when rows is empty, the FK query is skipped, `games_by_id` stays empty, but `applied_includes = ["game"]` is still echoed. That's the correct contract (include was requested, just nothing to expand), but no test verifies this.

**Severity:** SEV-3 — branch coverage gap. Specifically: test for `?include=game&offset=99999` (empty page) asserting `applied_includes == ["game"]` AND no rows.

---

### S3-P — Manifest row with missing parent game (CASCADE drift / partial migration) — silent `game: null`
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/manifests.py:212-214`

**Behavior trace:** when `?include=game` is set but the games row referenced by `manifests.game_id` is missing (FK violation; should be impossible per CASCADE but possible during partial migration or after manual DB surgery), `games_by_id.get(row["game_id"])` returns `None`, and the manifest is emitted with `game: null` — indistinguishable from "include not requested".

**Severity:** SEV-3 — silent data-integrity drift. The router should log a warning when this happens (it's a "should never happen" event, the kind that operators absolutely need to see in logs the day it does happen).

**Tests miss:** no test deletes a games row while leaving its manifest in place (with FK constraints disabled) and verifies the response shape + that a structured log fires.

---

## SEV-4 findings

### S4-A — `build_where_clause` with hand-crafted `{"in": []}` would emit `field IN ()` (invalid SQL)
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:342-345`

**Branch:** `placeholders = ", ".join("?" for _ in value)`. If `value == []`, `placeholders == ""` and the SQL fragment becomes `field IN ()` — syntactically invalid in SQLite.

The user-facing parser (`parse_filters`) can't produce this because `"".split(",")` yields `[""]` not `[]`. But the helper is a library surface; a future caller composing filter dicts by hand could trigger it. Untested.

**Severity:** SEV-4 — defensive robustness gap.

---

### S4-B — `_RESERVED_PARAM_NAMES` skip is by exact match; `?Limit=...` (capital L) falls through
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:277`

**Branch:** the filter loop skips keys in `_RESERVED_PARAM_NAMES = {"limit", "offset", "sort", "include"}` by exact match. `?Limit=10` (capital L) bypasses both the pagination parser (which looks up `params.get("limit")`, lowercase) and the reserved-param skip → falls through to `parse_filters`, where it's checked against the field allow-list → "unknown filter field: Limit" → 400.

So the user gets a 400 not a 200 — fine — but the failure mode is non-obvious. And future allow-lists might collide.

**Severity:** SEV-4. Worth a test pinning the 400 path.

---

### S4-C — `metadata: dict[str, Any] | None` — if DB returns a JSON list `[1,2,3]` for metadata, becomes `None` silently
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/games.py:230` (`metadata = parsed if isinstance(parsed, dict) else None`)

**Branch:** `metadata = parsed if isinstance(parsed, dict) else None`. Same pattern for `payload` in jobs.py. Lists/strings/numbers as the JSON root → coerced to `None`. Currently tested only for the malformed-JSON case (`test_malformed_metadata_returns_null`). The list/scalar case is **not** tested for games; it **is** tested for jobs (`test_non_dict_payload_returns_null`).

**Severity:** SEV-4 — minor parity gap. Add an equivalent test for games.

---

### S4-D — `LAST_ERROR_TRUNCATE = 200`: boundary not tested with exactly 200-char input
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/games.py:242`, `jobs.py:204`

**Branch:** `raw_err[:LAST_ERROR_TRUNCATE]` — a 200-char input is unchanged; a 201-char input becomes 200. Tests use `"x" * 5000` only. No test pins the exact boundary (200 → 200, 201 → 200, 199 → 199).

**Severity:** SEV-4.

---

### S4-E — `JSONResponse(content=body.model_dump(by_alias=True))` — `extra="forbid"` on response models has no runtime effect
**Files:** `games.py:78`, `jobs.py:75`, `manifests.py:74,80,99,111`

**Behavior:** `extra="forbid"` governs **input validation** (model construction). Once the router constructs a `GameResponse(...)` with named arguments, `extra` doesn't apply — there's no extra field surface to forbid. The setting is harmless but cosmetic at this layer. No test verifies the constraint actually rejects an attacker-crafted *response*, because there is no such surface.

**Severity:** SEV-4 — documentation/expectation gap. The `extra="forbid"` is correctly placed for OpenAPI / schema documentation purposes but its enforcement is at the schema layer, not the route handler. Worth a comment in the code.

---

### S4-F — No concurrent-request test for any of the three endpoints
**Files:** all three router test modules.

**Behavior:** the test suite is single-request per test. Pool implementation has its own concurrency tests, but the routers' interplay with the pool under concurrent reads is not exercised in the API test layer. Specifically: two simultaneous `?include=game` requests against the same data, two simultaneous large-offset queries, etc.

**Severity:** SEV-4 — common gap for read-only endpoints; lower-risk than write endpoints. Worth one or two `asyncio.gather` tests in each router.

---

### S4-G — `limit=1` boundary not tested per-endpoint
**Files:** all three router tests.

**Behavior:** `parse_pagination` enforces `limit >= 1`. The unit-level `test_zero_limit_raises` covers the lower-bound rejection. No integration test issues `?limit=1` to verify a single-row response shape + `has_more=True` when more rows exist.

**Severity:** SEV-4.

---

### S4-H — Direction case `?sort=title:DESC` (uppercase) works but is untested
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:420` (`direction = direction.strip().lower()`)

**Branch:** direction is `lower()`'d before validation, so `ASC`/`DESC`/`Asc` all work. Untested at any layer.

**Severity:** SEV-4. Small but useful regression pin.

---

### S4-I — `?sort= ` (single space) — `raw` is truthy (`" "`), splits to `[" "]`, entry empty after strip → loop skips → user_sort empty → default applied
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py:413-437`

**Branch trace:** `raw = " "`, the `if raw:` check is True (non-empty string), splits to `[" "]`, `entry.strip() == ""` skipped, `user_sort == []` → default applies. Same code path as S3-F but with `?sort=%20` instead of `?sort=,,,`. Untested.

**Severity:** SEV-4.

---

### S4-J — `tests/api/test_query_helpers.py:84-85` filter allow-list uses `value_type=str` for timestamp fields, divergent from production routers
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/tests/api/test_query_helpers.py:84-85`

**Branch:** the test fixture `_games_allow_list()` declares `last_prefilled_at` and `last_validated_at` with `value_type=str`. The **production** `games.py:48-49` uses `value_type="timestamp"`. The unit tests for filter parsing therefore don't exercise the timestamp validator path through `parse_filters` directly — only the timestamp-string validator's unit-level rejection via `_validate_timestamp_string` (which isn't directly tested either; only indirectly through router integration tests that hit `?started_at_gte=<script>...`).

**Severity:** SEV-4 — fixture drift from production. The unit suite would not catch a regression where `parse_filters` skipped the typed-string validator entirely.

**Recommendation:** update the unit fixture to match production, OR add explicit unit tests with `value_type="timestamp"` to cover the validator dispatch.

---

### S4-K — `pool.read_one` returning `None` vs `{"total": 0}` path partially tested
**Files:** `games.py:210`, `jobs.py:176`, `manifests.py:187` (`total = int(count_row["total"]) if count_row else 0`)

**Branch:** `if count_row` short-circuit. The COUNT(*) query always returns one row in SQLite (the count, even if 0). A mock pool that returns `None` would exercise the `else 0` branch. The pool-failure tests raise `PoolError` instead, so this defensive branch is never exercised. Dead-code-ish but defensive.

**Severity:** SEV-4 — branch coverage gap. Either remove the defensive branch (since COUNT always returns) or add a fake-pool test returning `None` from `read_one`.

---

### S4-L — `applied_includes` is `sorted(includes)` (set → sorted list), but with only one allowed key the sort is unobservable
**File:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/manifests.py:233`

**Behavior:** `applied_includes = sorted(includes)`. With one key ("game") this is always `["game"]` or `[]`. Future addition of a second include key would exercise the sort, but the spec D8 invariant ("deduped + sorted") is not actually exercised by any test today.

**Severity:** SEV-4. Add a synthetic test with a multi-key fixture allow-list, OR wait until a second include key is added and pin the sort then.

---

## Coverage map

Mapping each router's branches to test coverage:

| Branch | games | jobs | manifests |
|---|---|---|---|
| Happy path 200 | ✅ | ✅ | ✅ |
| Empty DB | ✅ | ✅ | ✅ |
| Default limit/offset | ✅ | ✅ | ✅ |
| `?limit=` valid | ✅ | ✅ | ✅ |
| `?limit=1` (boundary) | ❌ S4-G | ❌ S4-G | ❌ S4-G |
| `?limit=0` rejected (400) | indirect | indirect | indirect |
| `?limit>MAX` 400 | ✅ | ✅ | ✅ |
| `?offset=-1` 400 | ✅ | indirect | indirect |
| `offset > total` (empty page) | ❌ S3-A | ❌ S3-A | ❌ S3-A |
| `_eq` filter | ✅ | ✅ | ✅ |
| `_in` filter happy path | ✅ | ✅ | ✅ |
| `_in` empty value `?x_in=` | ❌ S3-B | ❌ S3-B | ❌ S3-B |
| `_in` 100-value boundary | ❌ S3-C | ❌ S3-C | ❌ S3-C |
| `_in` 101-value 400 | ❌ S3-C | ❌ S3-C | ❌ S3-C |
| `_in` dup values | ❌ S3-B | ❌ S3-B | ❌ S3-B |
| `_gte`/`_lte` happy | ✅ | ✅ | ✅ |
| `_gte`+`_lte` combined echo shape | ❌ S3-D | ❌ S3-D | ❌ S3-D |
| Inverted range (gte>lte) | ❌ S3-E | ❌ S3-E | ❌ S3-E |
| Multi-same-key (`?platform=a&platform=b`) | ❌ S3-I | ❌ S3-I | ❌ S3-I |
| Unknown filter field 400 | ✅ | ✅ | ✅ |
| Unknown op 400 | ✅ | ✅ | ✅ |
| Type-mismatch 400 | ✅ | indirect | indirect |
| Invalid timestamp 400 | indirect | ✅ | ✅ |
| `_gte` with `inf`/`nan` float | ❌ S3-L | ❌ S3-L | n/a |
| Negative for unsigned fields | ❌ S3-M | ❌ S3-M | ❌ S3-M |
| Default sort | ✅ | ✅ | ✅ |
| Explicit sort | ✅ | ✅ | ✅ |
| Tie-breaker dedup (single-field user sort) | ✅ | ✅ | ✅ |
| Tie-breaker dedup (multi-field user sort) | ❌ S3-G | ❌ S3-G | ❌ S3-G |
| Same sort field twice | ❌ S3-H | ❌ S3-H | ❌ S3-H |
| `?sort=,,,` falls back to default | ❌ S3-F | ❌ S3-F | ❌ S3-F |
| `?sort= ` (whitespace) | ❌ S4-I | ❌ S4-I | ❌ S4-I |
| Uppercase direction `?sort=title:DESC` | ❌ S4-H | ❌ S4-H | ❌ S4-H |
| Unknown sort field 400 | ✅ | ✅ | ✅ |
| Invalid direction 400 | indirect | indirect | indirect |
| Auth missing 401 | ✅ | ✅ | ✅ |
| Auth valid 200 | ✅ | ✅ | ✅ |
| Auth non-ASCII bytes | ❌ S2-A | ❌ S2-A | ❌ S2-A |
| Auth very-long token | ❌ S2-A | ❌ S2-A | ❌ S2-A |
| Pool error 503 | ✅ | ✅ | ✅ |
| Pool read_one returns None | ❌ S4-K | ❌ S4-K | ❌ S4-K |
| metadata/payload null | ✅ | ✅ | n/a |
| metadata/payload malformed | ✅ | ✅ | n/a |
| metadata/payload oversized | indirect (no >cap test) | ✅ | n/a |
| metadata non-dict JSON root | ❌ S4-C | ✅ | n/a |
| Literal-column DB drift (status='garbage') | ❌ S2-B | ❌ S2-B | n/a |
| last_error truncation @ 200 | ✅ | ✅ | n/a |
| last_error truncation boundary @ 201 | ❌ S4-D | ❌ S4-D | n/a |
| `?include=game` happy | n/a | n/a | ✅ |
| `?include=` empty | n/a | n/a | ✅ |
| `?include=GAME` case | n/a | n/a | ❌ S3-J |
| `?include=game,,game` mixed empty | n/a | n/a | ❌ S3-K |
| `?include=game` empty rows | n/a | n/a | ❌ S3-O |
| Orphan manifest (missing game) | n/a | n/a | ❌ S3-P |
| `applied_includes` sort (multi-key) | n/a | n/a | ❌ S4-L |

---

## Summary

- **0 SEV-1** found.
- **3 SEV-2** found (auth byte-stripping, Literal-column drift to 500, len-on-non-buffer crash).
- **16 SEV-3** found — mostly contract-pinning gaps + edge cases the tests don't probe.
- **12 SEV-4** found — boundary tests, parity gaps, defensive branches.

The suite is solid on the happy paths and on the failure modes UAT-3 / UAT-4 already exercised. The gaps cluster around (a) input boundary conditions (`_in` empties, inverted ranges, special floats, negative ints), (b) the applied-echo contract shape (multi-op same field, ordering, multi-key includes), (c) defensive paths that "shouldn't happen" (Literal-column drift, orphan FK, dict-returning pool), and (d) auth input fuzzing (non-ASCII bytes, very long tokens).

None of the findings are exploitable today. The auth byte-stripping (S2-A) and Literal-column drift (S2-B) are the closest to production-impactful: S2-A normalizes attacker input silently which is a latent timing-equality surface; S2-B will become a hard outage the first time a migration adds an enum value without updating the API model.

Recommend prioritizing S2-A, S2-B, S2-C, S3-A, S3-B, S3-E, S3-F, S3-I as the highest-value test additions for UAT-5 remediation.
