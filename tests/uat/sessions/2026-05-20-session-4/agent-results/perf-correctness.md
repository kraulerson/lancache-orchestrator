# UAT-4 Performance + Correctness Audit
**Agent:** perf-correctness
**Date:** 2026-05-20
**Scope:** `GET /api/v1/games` (BL7) + `_query_helpers.py` + downstream pool semantics
**Persona:** senior backend engineer who has seen pagination + filter performance go bad in production. Looking for the next subtle bug, not the obvious one.

---

## A: Index utilization

Verified empirically with `EXPLAIN QUERY PLAN` against the live `0001_initial.sql` schema seeded with 2000 representative rows. The planner choice does not change qualitatively at higher row counts; what changes is the absolute cost of the SCAN + TEMP B-TREE branches.

| Filter + Sort (router-emitted SQL after tie-breaker) | Index used | Sort path | Notes / risk at 50K rows |
|---|---|---|---|
| (none) `ORDER BY title ASC, id ASC` (default) | none — `SCAN games` | TEMP B-TREE | Full read + full sort. ~50K rows × ~500 B = ~25 MB into temp store (we set `temp_store=MEMORY`). Sub-200 ms expected. |
| (none) `ORDER BY size_bytes DESC, id ASC` | none — `SCAN games` | TEMP B-TREE | Same as default. Spec §4.3 acknowledges this; "add `idx_games_size_bytes` only if it shows up slow." Fine. |
| (none) `ORDER BY last_prefilled_at DESC, id ASC` | **none — `SCAN games`** | **TEMP B-TREE** | **Spec §4.3 is wrong here.** Spec claims `idx_games_last_prefilled` is used; planner does NOT use the partial index for an unqualified sort because NULL rows would be missing. At 50K rows the Game_shelf "Recently Prefilled" panel (very common) does a full scan + full sort. See SEV-3. |
| `WHERE last_prefilled_at >= ? ORDER BY last_prefilled_at DESC, id ASC` | **`idx_games_last_prefilled`** (SEARCH `> ?`) | TEMP B-TREE for the id tie-breaker | The filter qualifies the partial-index predicate (`IS NOT NULL` is implied by `>=`), so the index is finally usable. Index seek + small TEMP B-TREE for tie-break. Fast. |
| `WHERE status = ? ORDER BY title ASC, id ASC` | `idx_games_status` (SEARCH) | TEMP B-TREE | Index seek → selectivity-bounded row set → sort that subset. Sub-50 ms at 50K rows for a selective status. |
| `WHERE platform = ? ORDER BY title ASC, id ASC` | `idx_games_platform_app` (SEARCH on leading column) | TEMP B-TREE | Same shape; platform is binary (`steam`/`epic`), so the post-filter set is ~50% of the table — TEMP B-TREE still gets large. |
| `WHERE platform = ? AND status = ?` | `idx_games_status` (planner chose status as more selective; `platform` re-checked at row) | TEMP B-TREE | Planner's choice is reasonable. Confirmed empirically. |
| `WHERE status IN (?, ?, ?) ORDER BY last_prefilled_at DESC, id ASC` | `idx_games_status` (SEARCH per value) | TEMP B-TREE | OR-walk of the index. Common Game_shelf pattern. |
| `SELECT COUNT(*) FROM games` (no filter) | **`SCAN games USING COVERING INDEX idx_games_status`** | n/a | Planner picks the smallest covering index for an unfiltered count (status is a short TEXT — narrowest index leaf size). Sub-ms even at 500K rows. SQLite does NOT have an O(1) row-count shortcut, so it does scan the index leaves. |
| `SELECT COUNT(*) WHERE status = ?` | `idx_games_status` covering | n/a | Index range count. Fast. |
| `SELECT COUNT(*) WHERE platform = ?` | `idx_games_platform_app` covering | n/a | Index range count. Fast. |

**Worst case (no filter + `?sort=size_bytes:desc` or `?sort=last_prefilled_at:desc`):**
- 5K rows: ~5–10 ms total. Fine.
- 50K rows: ~30–80 ms total (TEMP B-TREE in memory, single allocator pass). Acceptable for a single-orchestrator workload.
- 500K rows (hypothetical, well past MVP): ~300–800 ms; temp-store overhead may bleed past the in-memory budget into a temp file. At this scale, the spec's deferred `idx_games_size_bytes` and a dedicated non-partial `idx_games_last_prefilled_full` would be required.

**Pre-existing redundancy (out of scope but worth flagging):** `idx_games_platform_app` duplicates the auto-created `sqlite_autoindex_games_1` from the `UNIQUE(platform, app_id)` table constraint. Two B-trees, double write amp, no read benefit. Not BL7's bug — file as cleanup follow-up.

---

## B: Connection pool semantics

**Two acquisitions per request — not one.**

`pool.read_one(count_sql, ...)` and `pool.read_all(rows_sql, ...)` each enter `_checkout_reader()` independently (`pool.py:909` and `pool.py:924`). Each call:
1. `await self._readers.get()` — checkout from `asyncio.Queue(maxsize=8)`
2. Execute statement on that reader
3. `await self._readers.put(reader)` — return to queue

Between the two calls the connection is fully released and the next call must re-acquire. There is no batching, no `WITH RECURSIVE`, no `read_transaction()` wrapper.

**Implication 1 — pool capacity.** With default `pool_readers=8`, the endpoint consumes effectively 2× the request's apparent reader slot footprint when measured over a request lifetime. Concurrent saturation point:
- Sustained ≥8 simultaneous in-flight `/api/v1/games` requests: pool fully booked.
- Behavior on exhaustion: `await self._readers.get()` blocks indefinitely (the queue is unbounded-wait). There is **no timeout, no PoolBusy exception, no 503**. A burst of requests just queues up at the asyncio layer. Per-request latency grows linearly with depth. The fastapi/uvicorn worker can absorb plenty before client disconnect, but a runaway loop on the consumer side would manifest as slow responses, not a clean failure.
- For a single-user orchestrator (the operator + Game_shelf SPA pulling 1 list at a time), this is fine. **For automated polling** (Game_shelf doing N parallel filtered fetches to render a dashboard), 8 concurrent requests starts to back-pressure. Worth noting in operator docs.

**Implication 2 — connection-affinity is broken.** Because the COUNT and the SELECT use different reader connections, they cannot share a transaction. SQLite WAL mode gives each reader connection a snapshot-isolated view as of the first read on that connection, but each new acquisition opens a fresh snapshot. **Two different reader connections can therefore see two different points in time.**

**Implication 3 — the race window (relevant to D below).** Sequence:
```
t0  COUNT acquires reader-A, sees snapshot S0, computes total=487, releases A
t1  ── here a writer commits, replicating to WAL frame; readers' next acquire will see S1
t2  SELECT ... LIMIT ? OFFSET ? acquires reader-B, sees snapshot S1, returns 50 rows
```
At t1 the row count may have changed. The response can report `total=487, len(games)=50` where the 50 rows came from a 488-row table. `has_more = offset + len(games) < total` is computed against a stale `total`. **Wire-visible artifact:** `total` and the rows can disagree by one or more depending on writes between t0 and t2. Single-user orchestrator + F3 daily sync → negligible odds in practice, but it IS observable.

**Verdict: SEV-3.** Acceptable for MVP. Convention to fix in a follow-up: route both reads through `pool.read_transaction()` to share one reader connection AND one snapshot. That makes the COUNT + SELECT trivially consistent and halves reader-pool consumption per request. Recommend doing this BEFORE the convention propagates to `/jobs`, `/manifests`, etc., where row velocity is much higher.

---

## C: Memory profile

**Per-row wire size estimate (200-char `last_error` worst case, typical otherwise):**

| Field | Typical bytes | Worst case |
|---|---|---|
| `id` | 4 (digits) | 8 |
| `platform` | 7 (`"steam"`) | 7 |
| `app_id` | 8–10 | 66 (length cap is 64) |
| `title` | 30 | ~200 (long titles exist; "Sid Meier's Civilization VI: Anthology" etc.) |
| `owned` | 1 | 1 |
| `size_bytes` | 12 (digits) | 14 |
| `current_version`, `cached_version` | 40 each | 80 each |
| `status` | 18 (`"validation_failed"`) | 18 |
| `last_validated_at`, `last_prefilled_at` | 20 each (ISO 8601) | 20 each |
| `last_error` | 0–200 | 200 |
| `metadata` | 100–500 (depot lists) | bounded by SQLite TEXT — see below |
| JSON keys + structural | ~140 | ~140 |
| **per-row total** | ~400 B | ~900 B |

**Response sizes:**
- `limit=50` (default): ~20 KB.
- `limit=500` (max): ~200–450 KB. Acceptable.
- Hypothetical `limit=10000`: 4–9 MB. The 500 Pydantic model construction × 20 ≈ 10K instances. At ~1.5 KB per Pydantic v2 instance retained (after the new core), that's ~15 MB of heap held simultaneously plus the JSON encode buffer. **This is why `MAX_LIMIT=500` is the right guard.**

**`_query_helpers.parse_pagination` enforces only what the caller passes.** The helpers themselves do NOT enforce a defensive ceiling lower than the endpoint declares. That is the right separation (helpers are policy-free) — but it does mean a future endpoint declaring `max_limit=10000` would be self-DoS-able from inside the trust boundary. Recommend adding a module-level `ABSOLUTE_MAX_LIMIT = 1000` cap inside `parse_pagination` and refusing any caller-passed `max_limit > ABSOLUTE_MAX_LIMIT` with a `QueryParamError`. SEV-4.

**`applied_filters` echo size — `FilterCriterion` serialization defect:**

This is the big one. Empirically verified by constructing `FilterCriterion(eq="steam")` and dumping it:

```
{"eq": "steam", "in": null, "gte": null, "lte": null, "gt": null, "lt": null, "ne": null}
```

The response model emits **all seven operator keys per filtered field**, six of them `null`. The spec §3.2 example shows the compact `{"platform": {"eq": "steam"}}` shape. The actual wire deviates from the spec.

- Per filtered field overhead: ~80 bytes of null operator keys (vs ~15 bytes for just the populated op).
- With 6 fields filtered, that's ~500 bytes of pure nulls in `applied_filters`.
- With `_in` having 100 values × 6 fields filtered (hypothetical), the data is dominated by the IN list (good), but the null operator keys are still ~500 bytes pure waste.

**This is SEV-2** — contract drift between spec and implementation, locks the wrong convention for every future paginated F9 endpoint, and bloats every response with `applied_filters` set. Fix: in `routers/games.py:262`, change `body.model_dump(by_alias=True)` to `body.model_dump(by_alias=True, exclude_none=True)`. Alternative: add `model_config = ConfigDict(extra="forbid", populate_by_name=True, json_schema_extra={"exclude_none": True})` — but `exclude_none` is a dump option, not a Config field. The cleanest fix is at the `model_dump` call.

**Caveat — `exclude_none=True` interaction with `last_error`/`metadata`/`size_bytes` etc.** Many `GameResponse` fields are `T | None` and a `None` is a legitimate wire value. Naive `exclude_none=True` at the top level will drop them — clients then can't distinguish "field absent because null" from "field truly absent in this model version." The right fix is per-model: dump `FilterCriterion` with `exclude_none=True` but leave `GameResponse` alone. Easiest implementation: build the `applied_filters` dict by hand (e.g. `{"platform": {"eq": "steam"}}`) and skip the FilterCriterion wrapper entirely for serialization — keep it only for OpenAPI schema documentation. Document in the spec which convention won.

---

## D: Pagination drift under concurrent writes

**The drift window has two layers:**

**Layer 1 — within a single request** (analyzed in §B). Because COUNT and SELECT acquire separate reader connections with separate WAL snapshots, a writer between them can produce a response where `total` is off by ±N from the actual SELECT row count. Single-user orchestrator: negligible. Convention-setting concern.

**Layer 2 — across requests by the same client** (the classic offset-drift problem):

```
F3 library_sync starts; inserts 200 new Steam games sorted alphabetically (Aphelion … Crysis Remastered)
t0  Client GET /games?sort=title:asc&limit=50&offset=0    → ["Aardvark", … "Borderlands 3"] (50 rows)
t1  F3 inserts "Abzu" (now sorts second)
t2  Client GET /games?sort=title:asc&limit=50&offset=50   → 50 rows starting at the 51st of CURRENT state
```

Because "Abzu" is now in offset position 2, every row at offset ≥ 2 has shifted by one. The client's page-2 fetch (offset=50) returns the row that was at offset 49 in the original universe — i.e. one of the rows it already saw on page 1. **Same row appears on two consecutive pages.** Conversely, if F3 had DELETED a row at the front, page 2 would SKIP a row.

**`id:asc` tie-breaker prevents WITHIN-PAGE row dup on tied sort values** (e.g. two games with identical `last_prefilled_at` and same offset boundary). It does NOT prevent BETWEEN-PAGE drift caused by inserts/deletes/sort-key updates landing in the offset range.

**Worst-case drift in this codebase:**
- F3 library_sync batches up to a few hundred upserts per platform per run. Typical runs are daily and complete in 30–120 seconds.
- A Game_shelf user paginating through 500 rows at 50/page (10 fetches) over ~30 seconds while F3 is mid-sync could observe 5–20 duplicated or skipped rows in the most adversarial case (F3 happens to be inserting alphabetically into the range the user is scrolling).
- Realistic case (F3 not running): zero drift.
- Realistic case (F3 running but user not actively paginating): zero drift.

**Verdict: SEV-3.** Acceptable for MVP per spec §6 risk register. Document the convention so Game_shelf doesn't assume strict snapshot semantics. The proper fix (cursor-based pagination keyed on `(sort_key, id) > last_seen`) is reserved for `/jobs` if/when retention grows. Add a brief note to `_query_helpers.py` module docstring describing the drift semantics so future endpoints inherit the same understanding.

---

## E: Metadata JSON parse cost

**Typical metadata payload** (per spec §6 / Phase 1 data-model): depot list + build hints. Realistic shape:
```json
{"depots": [1086941, 1086942, 1086943], "build": "manifest_gid_xxx", "branch": "main"}
```
~100–200 bytes. `json.loads` on a row of this size: ~3–8 µs on a modern machine.

**At limit=500:**
- 500 rows × 5 µs ≈ 2.5 ms total parse cost. Negligible.

**Pathological case** (1 MB metadata blob, e.g. a future F3 bug that stuffs the full Steam app manifest into the column):
- 500 rows × ~30 ms parse = 15 s of CPU before response is built. Pydantic model construction with a 1 MB `dict[str, Any]` adds another ~10 ms per row.
- Wire response: ~500 MB. The 200 KB target turns into 500 MB. JSONResponse builds the full body in memory before sending.
- This would manifest as request timeouts, OOM, or both. There is no defensive cap on `metadata` size in the router.

**Defensive recommendation (SEV-4):** Add a soft cap. Either:
- `len(raw_meta) > 64 KB` → log `api.games.metadata_too_large` and return `None` (treat like a parse failure).
- OR cap at the DB write side via a CHECK constraint in a future migration (`length(metadata) <= 65536`). Schema-level is better — applies to F3 directly.

**Log volume risk on corrupt rows:** `_log.warning("api.games.metadata_parse_failed", game_id=...)` fires once per row per request. With 500 corrupt rows in the result set, that's 500 log lines per request. At even 10 RPS this is 5K log lines/sec — observability noise that hides real signals. Recommend: rate-limit the warning (one per request, aggregating count + sample game_ids), or downgrade to debug-level and rely on a CHECK constraint to surface corruption at write time. SEV-4.

---

## F: OpenAPI schema correctness

**Verified empirically** via `FilterCriterion.model_json_schema()` and `GameListResponse.model_json_schema()`:

- `in_` field with `alias="in"` renders as `"in"` in the schema property name. The alias is resolved correctly because the model has `populate_by_name=True` and `model_dump(by_alias=True)` is used at the router.
- `additionalProperties: false` is set on every model declared with `extra="forbid"` (`GameResponse`, `FilterCriterion`, `SortFieldResponse`, `GamesMeta`, `GameListResponse`). Schema-drift detection works.
- BUT: each operator field is typed `anyOf [{}, null]` (i.e. `Any | None`) in the schema. This is **technically accurate** for the model but **operationally useless** for clients — the schema doesn't tell a Game_shelf consumer what type `eq` will be for the `platform` field (string enum) vs `size_bytes` field (int). The OpenAPI doc doesn't distinguish. This is a minor surface defect inherent in modeling `FilterCriterion` as a generic across-field shape. SEV-4 — out of scope to fix in BL7 (would require per-field FilterCriterion subclasses, a contract-explosion not worth it for a debug-echo).

**Surprise — the schema documents fields that NEVER appear in production responses:**

Because of the SEV-2 defect in §C (all seven keys emitted with nulls), the schema's `properties` listing of `eq, in, gte, lte, gt, lt, ne` *does* match the wire — except every null field on the wire is documented as `anyOf [{}, null]` with default null. A spec-conformant client reading the OpenAPI doc will write code that handles all seven keys per criterion, which "happens to" match the (buggy) wire. **Fixing the SEV-2 defect with `exclude_none=True` will create a SCHEMA-VS-WIRE DRIFT**: schema still lists the seven keys; wire only sends populated ones. Most clients tolerate this (additionalProperties=false on receive doesn't apply to absent keys), but strict schema-validators that require all declared properties will reject. Recommend marking unused operator fields as not-required (Pydantic does this by default since they have `default=None`), which is already the case. So `exclude_none=True` is safe.

**Verdict:** Schema is correct and matches current (buggy) wire. The schema will REMAIN correct after the `exclude_none` fix because all operator fields default to None (i.e. are not required). No schema change needed.

---

## G: Test coverage gaps + suggested additions

**Existing tests** cover each axis (pagination, filter-per-field, sort, applied-echo, errors, auth, pool failure, metadata, last_error truncation) in isolation. Cross-combination + correctness-under-load gaps:

**G1. Cross-combination test — filter + sort + pagination together, with `applied_*` echo audit.**
```python
async def test_filter_sort_pagination_combined(self, client, games_pool_100):
    """Compound query: filter by platform AND status range, sort multi-field,
    paginate. Verify (a) row order, (b) total count matches filter, (c) echo
    reflects all parsed params, (d) has_more is correct."""
    r = await client.get(
        "/api/v1/games?platform=steam&size_bytes_gte=1000000000"
        "&sort=last_prefilled_at:desc,title:asc&limit=10&offset=10",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    body = r.json()
    # All rows match the compound filter
    for g in body["games"]:
        assert g["platform"] == "steam"
        assert g["size_bytes"] >= 1_000_000_000
    # Sort order respected (and tie-breaker present)
    titles_grouped_by_prefill = ...  # verify desc on prefilled_at, asc on title within
    # Echo reflects EVERYTHING parsed
    assert body["meta"]["applied_filters"]["platform"]["eq"] == "steam"
    assert body["meta"]["applied_filters"]["size_bytes"]["gte"] == 1_000_000_000
    assert body["meta"]["applied_sort"] == [
        {"field": "last_prefilled_at", "direction": "desc"},
        {"field": "title", "direction": "asc"},
        {"field": "id", "direction": "asc"},
    ]
    # has_more arithmetic
    assert body["meta"]["has_more"] == (10 + len(body["games"]) < body["meta"]["total"])
```
**Would catch the SEV-2 `applied_filters` shape bug** because it asserts equality on the dict (current implementation would fail because the dict actually has six extra null keys per criterion).

**G2. Pagination consistency across pages — no duplicates, no gaps.**
```python
async def test_pagination_full_walk_no_dup_no_skip(self, client, games_pool_100):
    """Walk all 100 rows in 10-row pages. Concatenated IDs must equal the
    full unfiltered ID set with no duplicates and no skips."""
    seen = []
    for offset in range(0, 100, 10):
        r = await client.get(
            f"/api/v1/games?limit=10&offset={offset}&sort=title:asc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids = [g["id"] for g in r.json()["games"]]
        seen.extend(ids)
    assert len(seen) == 100
    assert len(set(seen)) == 100  # no duplicates
    # And the tie-breaker means a tied-title row set is still stable: re-fetch
    # any page and IDs must match.
    r2 = await client.get(
        "/api/v1/games?limit=10&offset=40&sort=title:asc",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert [g["id"] for g in r2.json()["games"]] == seen[40:50]
```
Catches: (a) off-by-one in offset/limit math, (b) tie-breaker not actually being applied (would manifest as page re-fetch returning a different ordering when titles tie), (c) regressions in the SQL builder that drop the ORDER BY.

**G3. Determinism of `applied_sort` echo with multi-field input.**
```python
async def test_applied_sort_echo_preserves_order(self, client, games_pool_100):
    """The applied_sort echo must preserve the user's specified order, with
    the tie-breaker appended LAST (not interleaved). Run twice — order must
    be identical (no dict-ordering hazard)."""
    for _ in range(2):
        r = await client.get(
            "/api/v1/games?sort=status:asc,size_bytes:desc,title:asc&limit=1",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.json()["meta"]["applied_sort"] == [
            {"field": "status", "direction": "asc"},
            {"field": "size_bytes", "direction": "desc"},
            {"field": "title", "direction": "asc"},
            {"field": "id", "direction": "asc"},
        ]
```
The current implementation uses a `list[SortField]`, so order is stable by construction — but pin it with a test so a future refactor to a set/dict doesn't silently lose it.

**G4 (bonus).** Repeated query-param keys: `?platform=steam&platform=epic` — verify documented behavior (Starlette returns first; the second is silently dropped). Currently undocumented and untested. Either pin the behavior with a test or raise `QueryParamError` on duplicates.

---

## Findings

### SEV-1
*(none)*

### SEV-2
- **`applied_filters` echo wire format does not match spec §3.2.** The `FilterCriterion` model emits all seven operator keys per filtered field, six as `null`. Spec example shows compact `{"eq": "steam"}`. Fix at `routers/games.py:262` — change to `body.model_dump(by_alias=True, exclude_none=True)` BUT scope the exclusion to `applied_filters` only (don't drop legitimate `None`s in `GameResponse` fields). Cleanest implementation: build `applied_filters` as a plain `dict[str, dict[str, Any]]` and skip the FilterCriterion wrapper entirely for output (keep it for OpenAPI documentation). Locks the right convention for every future paginated F9 endpoint. Test G1 above catches this.

### SEV-3
- **Spec §4.3 index-utilization claim is wrong for `?sort=last_prefilled_at:desc` (no filter).** The partial index `idx_games_last_prefilled WHERE last_prefilled_at IS NOT NULL` is NOT used by an unqualified `ORDER BY last_prefilled_at DESC`. The planner does `SCAN games + USE TEMP B-TREE FOR ORDER BY`. At 5K rows it's fine; at 50K+ rows the "Recently Prefilled" panel (a very common Game_shelf view) does a full table sort. Fix one of: (a) make the index non-partial in a follow-up migration, (b) update the spec/docs to be accurate about when the index applies (i.e. only with a `last_prefilled_at >= ?` filter), or (c) document that the canonical "recently prefilled" query MUST include `?last_prefilled_at_gte=<some_floor>` to engage the index — and have Game_shelf send it.
- **COUNT + SELECT cross-snapshot inconsistency.** Two separate reader-pool acquisitions mean two WAL snapshots — `total` and the row set can disagree by ±N if a writer commits between them. Negligible probability for single-user MVP, but the convention locks here. Recommended follow-up: route both reads through `pool.read_transaction()` to share one connection and one snapshot. This also halves reader-pool consumption per request.
- **Offset pagination drift under concurrent F3 writes.** Documented acceptable in spec §6 risk register. Worst case during F3 sync mid-pagination: 5–20 duplicated/skipped rows across a 500-row walk. Tie-breaker prevents in-page dup but not cross-page drift. Add a brief note to `_query_helpers.py` module docstring so this is the documented convention.

### SEV-4
- **No defensive `ABSOLUTE_MAX_LIMIT` ceiling in `parse_pagination`.** Helper trusts the caller's `max_limit`. A future endpoint declaring `max_limit=100000` is self-DoS-able. Add a module-level cap (e.g. 1000) and raise `QueryParamError` if the caller passes a higher `max_limit`.
- **`metadata` column has no defensive size cap.** A future write of a 1 MB blob to one row would inflate a 500-row response to ~500 MB. Cap at the schema level via a CHECK constraint in a follow-up migration, or soft-cap in the router (treat oversize as parse failure).
- **`api.games.metadata_parse_failed` warning fires once per row.** 500 corrupt rows = 500 log lines per request. Rate-limit or aggregate (one log per request with count + sample IDs).
- **OpenAPI schema documents `FilterCriterion` operator fields as `anyOf [{}, null]`.** Type information is lost. Out of scope to fix — per-field FilterCriterion subclasses are not worth the contract complexity for a debug echo. Noted only.
- **Repeated query-param keys** (`?platform=steam&platform=epic`) silently keep first value. Undocumented. Pin behavior with a test or raise `QueryParamError`.
- **Test coverage gaps G1–G4** above (cross-combination, pagination full walk, applied_sort order determinism, repeated keys).

---

## Non-findings

- **SQL injection.** `_query_helpers.build_where_clause` and `build_order_by_clause` interpolate ONLY allow-list-validated field names; user values flow through `?` placeholders. Defensive re-check at builder layer (`build_where_clause` re-validates field names against the allow-list even though the parser already did) is good belt-and-suspenders. `TestSqlInjectionResistance::test_build_where_never_interpolates_values` (Hypothesis property test) pins this.
- **`extra="forbid"` schema strictness.** Correctly applied on every response model; `additionalProperties: false` reflected in OpenAPI schema. Schema-drift will be caught at CI.
- **`limit > MAX_LIMIT` clamp behavior.** Loud rejection (400) per D3, not silent clamp. Operator catches their own mistakes.
- **`tie_breaker` de-duplication when user explicitly sorts by `id`.** `parse_sort` correctly omits the appended tie-breaker if the user's sort already includes `id` in any direction. Verified in `test_tie_breaker_deduplicated_when_user_sorts_by_id`.
- **Suffix detection longest-first.** `gte` checked before `gt`, `lte` before `lt` — correct. No risk of `_gt` eating the suffix of a `field_gte` key.
- **`status` Literal in `GameResponse`.** Matches the schema CHECK constraint exactly. Adding a new status will require updating both in the same migration — risk noted in spec §6 risk register.
- **`last_error` truncation.** 200-char cap with `raw_err[:LAST_ERROR_TRUNCATE]`. Handles None correctly. Defense-in-depth on top of upstream scrubbing — exactly the BL6 pattern.
- **Pool `PoolError` → 503 mapping.** Consistent with `/health` and `/platforms`. Structured `api.games.read_failed` log emitted. Test `TestGamesPoolFailure::test_pool_error_returns_503` pins this.
- **COUNT(*) on unfiltered query.** Uses `SCAN games USING COVERING INDEX idx_games_status` — planner picks the smallest covering index automatically. Sub-ms at 500K rows. Spec §6 risk "COUNT becomes slow on huge tables" is well-mitigated.
- **`json.loads` exception handling in metadata parse.** Catches both `JSONDecodeError` AND `TypeError` (the `isinstance(parsed, dict)` guard handles JSON values that parse to non-dict types like arrays/scalars). Sound.
