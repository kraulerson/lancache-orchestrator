# UAT-4 Threat Model Walk
**Agent:** threat-model
**Date:** 2026-05-20
**Persona:** Penetration Tester ("I have a bearer token — what do I do next?")
**Scope:** BL6 (`GET /api/v1/platforms`) + BL7 (`GET /api/v1/games`) surface on top of the UAT-3-hardened BL5 substrate.

---

## TM walks

### TM-001 — Bearer-token leak / unauthenticated access to BL6+BL7
**Walk.** I have stolen the bearer token (TM-023 step 4). First, I confirm I actually need it: I curl both endpoints without `Authorization`:

```
GET /api/v1/platforms                 -> expect 401
GET /api/v1/games?limit=5             -> expect 401
GET /api/v1/games/                    -> trailing slash variant -> still routed?
GET /api/v1/games?_=evade             -> noise param doesn't help
```

Grep the exempt path tuple (`src/orchestrator/api/dependencies.py:28-33`):

```python
AUTH_EXEMPT_PATHS = (
    ("/api/v1/health", False),
    ("/api/v1/openapi.json", False),
    ("/api/v1/docs", True),
    ("/api/v1/redoc", False),
)
```

Neither `/api/v1/platforms` nor `/api/v1/games` is present. `BearerAuthMiddleware.__call__` (middleware.py:236-242) does `path == exempt_path` or `path.startswith(exempt_path + "/")` — so `/api/v1/platforms` and `/api/v1/games` fall through to bearer enforcement. UAT-3 already nailed the substring-prefix evasion (`/api/v1/healthxxx`). `OPTIONS` is exempt for CORS preflight only — fine, no body is returned.

I cannot reach either endpoint without the token.

**Verdict: MITIGATED.**

---

### TM-005 — SQL injection at the BL6+BL7 endpoint surface
**Walk.** With the bearer, I try to break out of the query builder. BL6 has no user-controlled SQL whatsoever (`platforms.py:63-67` is a static string with a hard-coded `ORDER BY`). One identifier appears in the query (`'steam'`) but it is a literal in the source — not derived from input. Dead end at BL6.

BL7 is the interesting target. The endpoint composes three primitives from `_query_helpers.py`:

```python
where_sql, where_params = build_where_clause(filters, allow_list=GAMES_FILTER_ALLOW_LIST)
order_sql = build_order_by_clause(sort)
count_sql = f"SELECT COUNT(*) AS total FROM games {where_sql}".strip()
rows_sql = f"SELECT {_GAMES_COLUMNS} FROM games {where_sql} {order_sql} LIMIT ? OFFSET ?".strip()
```

Attack 1 — value-side payload:

```
GET /api/v1/games?platform=steam'; DROP TABLE games; --
```

`parse_filters` calls `_coerce_value(raw, str, ...)` which just returns the raw string. Then `build_where_clause` appends `{field} = ?` (line 223) and pushes the raw string into `params`. The driver (`aiosqlite`) parameter-binds it. The `'` and `;` never escape the bind value. Confirmed by `TestSqlInjectionResistance` Hypothesis property test.

Attack 2 — identifier-side payload:

```
GET /api/v1/games?platform);DROP--=steam
```

`parse_filters` derives field_name = "platform);DROP--", looks it up in `GAMES_FILTER_ALLOW_LIST.by_field`, miss → 400. Identifier interpolation is gated by allow-list membership; the defensive re-check in `build_where_clause:214-215` makes this a layered invariant.

Attack 3 — sort-side payload:

```
GET /api/v1/games?sort=title;DROP--:asc
```

`parse_sort` looks up `"title;DROP--"` in `GAMES_SORT_ALLOW_LIST.fields`, miss → 400. Direction is checked against the `{"asc","desc"}` set; "asc--" yields 400.

Attack 4 — `_in` boundary:

```
GET /api/v1/games?platform_in=steam,'OR1=1
```

`split(",")` produces ["steam", "'OR1=1"]; each passes through `_coerce_value(str, ...)` unchanged; both become parameter-bound values. The placeholders SQL is `platform IN (?, ?)`. Safe.

The router composes the helpers correctly. No string-format / no f-string of values into SQL. The `# noqa: S608` comments (games.py:182-185, 188) are accurate; the f-string only interpolates allow-list-validated tokens.

**Verdict: MITIGATED.**

---

### TM-011 — CORS misconfiguration / regression after UAT-3 reorder
**Walk.** Per UAT-3 (ADR-0012 addendum) CORS is now the OUTERMOST middleware. From `main.py:165-175`:

```python
app.add_middleware(BearerAuthMiddleware)   # innermost (registered first)
app.add_middleware(BodySizeCapMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(CORSMiddleware, ...)    # outermost (registered last)
```

Because Starlette prepends, the runtime order is CORS → CorrelationId → BodySizeCap → BearerAuth → routers. BL6+BL7 inherit this via `app.include_router(...)`. There is no per-router CORS override. CORS allow-origins comes from `settings.cors_origins` (not `*`). `allow_credentials=False`. Methods are an explicit list including GET (used by both endpoints).

Concrete preflight attempt from a hostile origin:

```
OPTIONS /api/v1/games
Origin: https://attacker.example
Access-Control-Request-Method: GET
```

`BearerAuthMiddleware.__call__:212-214` skips OPTIONS, so the preflight reaches the CORS layer cleanly. CORS rejects (origin not in allow-list); browser refuses to send the real GET. A non-browser client can still hit the endpoint, but it then needs the bearer (TM-001). No new vector at BL6+BL7.

A residual concern: CorrelationId is now inside CORS, so CORS-rejected preflights lack a correlation_id in logs. UAT-3 explicitly accepted this trade.

**Verdict: MITIGATED.**

---

### TM-012 — Credential redaction in `api.platforms.read_failed` / `api.games.read_failed`
**Walk.** I want to coerce sensitive row content into a log line.

BL6 (`platforms.py:69`):

```python
_log.error("api.platforms.read_failed", reason=str(e))
```

`e` is a `PoolError`. ADR-0011 declares PoolError messages must not contain SQL/params/credentials. The router never logs `row["last_error"]` or any other column — rows are returned in the JSONResponse body but never reach a log call. ID3 redaction also covers the chain if `e` accidentally has a sensitive key in its kwargs.

BL7 (`games.py:196` and `:211-214`):

```python
_log.error("api.games.read_failed", reason=str(e))
...
_log.warning("api.games.metadata_parse_failed", game_id=row["id"])
```

The `metadata_parse_failed` log emits only `game_id` (an integer PK). It does NOT include `raw_meta` (the malformed JSON string). Good — a hostile metadata value cannot smuggle itself into the log channel.

`last_error` is truncated to 200 chars and rendered in the response body, but it does NOT appear in any log call from the router. If `last_error` contained a Steam refresh token by mistake (data-layer bug), it would leak via the wire to an authenticated client, NOT via logs — out of TM-012 scope.

**Verdict: MITIGATED** for log-side. Wire-side disclosure of `last_error` is a data-handling concern noted in §Findings below.

---

### TM-013 — Differential responses as a fingerprinting / enumeration oracle
**Walk.** UAT-3 reduced this surface, but BL7's 400 messages are a new oracle. Three response shapes from BL7: 200, 400, 503. The 400 carries an attacker-controlled diagnostic string. I probe:

```
GET /api/v1/games?password=foo   -> 400 {"detail": "unknown filter field: password"}
GET /api/v1/games?platform=foo   -> 200 {... games: [], total: 0 ...}   (with applied_filters echo)
GET /api/v1/games?title=foo      -> 400 {"detail": "unknown filter field: title"}
GET /api/v1/games?status=foo     -> 200
GET /api/v1/games?id=42          -> 400 {"detail": "unknown filter field: id"}
```

So the message is the same shape — `unknown filter field: <name>` — for any non-allow-listed name. I learn nothing about whether `title` is a column; I only learn that it's not in `GAMES_FILTER_ALLOW_LIST`. That allow-list (`platform, status, owned, size_bytes, last_prefilled_at, last_validated_at`) is also visible in the OpenAPI bundle (loopback-only). The 400 doesn't distinguish "column does not exist" from "column exists but not filterable" — both produce the same message. Good.

Timing differential: 400 paths short-circuit before any SQL; 200 with `?status=foo` runs `COUNT(*)` + `SELECT`. A measurable delta exists (sub-millisecond on a small DB; tens of ms on the 2,600-row production DB), but it only confirms "this field is in the allow-list" — which is the same information you get from the response status code. No additional schema knowledge leaks via timing.

Schema enumeration via 400 messages is covered in detail in Beyond-TM scenario 1.

**Verdict: PARTIAL — see §Findings F1 (400-message field enumeration is a minor information disclosure).**

---

### TM-015 — Resource exhaustion via BL7 query path
**Walk.** I am authenticated. I want to DoS the DB or response pipeline.

Attack 1 — high limit:

```
GET /api/v1/games?limit=999999
```

`parse_pagination` rejects `limit > 500` with 400. Hard cap, enforced before SQL.

Attack 2 — `OFFSET` abuse:

```
GET /api/v1/games?limit=500&offset=99999999
```

`offset >= 0` is the only constraint. SQLite walks `offset` rows before returning — but a `WHERE` clause limits the work, and even on a 1M-row table SQLite's offset-walk on an indexed sort is sub-second. With our default sort by `title` (no index on `title`) it requires a full sort first, then offset/limit. For 1M rows, this is the pathology. Wall-clock estimate on DXP4800: 2–4 s. Not a runaway; not a 30-second hang; but a hot-loop of these requests can saturate the aiosqlite pool (10 connections, per TM-015 mitigation note). uvicorn's `limit_concurrency` will queue further requests.

Attack 3 — `COUNT(*)` pathology — the explicit ask:

```
GET /api/v1/games            # no filter, default limit
```

This always runs `SELECT COUNT(*) FROM games` (no WHERE) followed by the page query. On a 1M-row games table without partial indexes covering the predicate-free count, SQLite scans the entire `games` table or — if there's no compact covering index — uses the rowid index (fast). With STRICT mode and INTEGER PRIMARY KEY AUTOINCREMENT, `COUNT(*)` is roughly O(n) page-reads in the worst case. At 1M rows, ~50–100 ms on warm cache, maybe 500 ms cold. Not a 5-second hang.

Attack 4 — combined: `?last_validated_at_gte=2000-01-01` (a high-cardinality filter with no index). The filter is allow-listed, but `games.last_validated_at` has no index in the schema. SQLite full-scans + filters in-memory. On 1M rows this is the worst case: a couple of seconds per request. Burst 20 such requests in parallel → pool exhausts → uvicorn queues → no 5xx, just slow.

Realistic verdict: the BL7 endpoint will be slow under malicious load on a saturated DB but will not crash. The 503 path (PoolError) is the failure mode; pool exhaustion returns properly. No memory blowup because `limit ≤ 500` rows × ~2 KB row = ~1 MB max response.

**Verdict: MITIGATED for crash/exhaust; PARTIAL for sustained-slowdown. See §Findings F2.**

---

### TM-018 — Oversized request body on GET endpoints
**Walk.** Both endpoints are GET. I send:

```
GET /api/v1/games HTTP/1.1
Content-Length: 999999999

<gibberish body>
```

`BodySizeCapMiddleware` reads Content-Length and short-circuits 413 if > 32 KiB. So this is rejected before any handler runs. But — do the handlers actually call `request.body()`? `platforms.py` doesn't import `Request` at all. `games.py` uses `Request` only for `request.query_params`. No `await request.body()`. No `await request.json()`. The body is silently ignored by the routers.

Even if the cap layer were absent, the handlers wouldn't materialize the body. No oversized-body vector on GET.

**Verdict: MITIGATED.**

---

### TM-021 — Correlation ID injection / regression
**Walk.** UAT-3 made `CorrelationIdMiddleware` regenerate any malformed UUID. I send:

```
GET /api/v1/games
X-Correlation-ID: "><script>alert(1)</script>
```

middleware.py:67-68:

```python
cid_in = cid_bytes.decode("ascii", errors="ignore")
cid = cid_in if _UUID4_RE.match(cid_in) else str(uuid.uuid4())
```

`_UUID4_RE` is strict UUIDv4. The attacker payload fails regex → server generates a fresh UUID. The crafted value never appears in any log key (correlation_id) or response header. Both BL6 and BL7 inherit this via `app.include_router(...)`.

Bonus check: what if I send a perfectly-valid attacker-chosen UUIDv4 (e.g., `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa`)? That passes the regex and is honored. Threat impact: log-stitching — attacker can correlate their own requests across log lines. Not a confidentiality, integrity, or availability impact. Documented elsewhere (UAT-3 accepted residual risk).

**Verdict: MITIGATED.**

---

### TM-023 — Kill-chain step 6: `/api/v1/games` is NOT loopback-only
**Walk.** Per `dependencies.py:55-61`, the loopback-only patterns are `platforms/{name}/auth`, `openapi.json`, `docs`, `redoc`. `/api/v1/games` and `/api/v1/platforms` are bearer-only, LAN-reachable. This is the documented threat-model behavior — TM-023 step 6 explicitly notes "the orchestrator has no defense against a valid-bearer-token request."

With my stolen bearer from a compromised Game_shelf LXC, I:

1. `GET /api/v1/platforms` — confirm both platforms `auth_status: "ok"`, get sync timestamps.
2. `GET /api/v1/games?limit=500&offset=0`, then `?offset=500`, … walk the entire library in 6 requests (2,600 games ÷ 500). 
3. Cross-reference titles + sizes against the operator's public Steam profile to confirm identity.
4. Apply `?status=blocked` to learn which games the operator has hidden — a soft signal about gaming preferences/embarrassment.

All as designed. The kill chain is acknowledged in the threat model; no new mitigation is in BL6+BL7 scope.

**Verdict: PARTIAL / AS-DESIGNED.** Compensating control = pfSense host-restriction (Phase 3/4 ops) + Game_shelf .env hygiene.

---

## Beyond-TM scenarios

### Scenario 1 — Schema enumeration via 400 messages
**Walk.** I attack the allow-list disclosure. I write a dictionary attack iterating through 100 candidate field names:

```python
for name in ["title","id","app_id","platform","status","owned","size_bytes",
             "current_version","cached_version","last_validated_at",
             "last_prefilled_at","last_error","metadata","auth_status",
             "auth_method","auth_expires_at","last_sync_at","config",
             "name","password","admin","secret","steam_session"]:
    r = GET /api/v1/games?{name}=x
    if r.status == 200: known_filterable.add(name)
    elif r.status == 400 and "unknown filter field" in r.text: known_not_filterable.add(name)
```

I learn:
- `platform, status, owned, size_bytes, last_prefilled_at, last_validated_at` → filterable (200 returned).
- Every other name → 400 `"unknown filter field"`.

What does this reveal that's not already in the threat model? **Very little**, because:

1. The same information appears in `/api/v1/openapi.json` (loopback-only post-UAT-3 — attacker on LAN cannot reach it).
2. The codebase is open-source; the allow-list is grep-able from GitHub in ~10 seconds. Knowing this list does not advance the attacker.
3. The 400 does NOT distinguish "column exists but is not filterable" from "column does not exist". I cannot, from outside, tell whether `title` is a real column or a phantom — both give the same 400. The schema is NOT enumerated; only the filter allow-list is.

So: minor disclosure (the filter allow-list, which is also public via source code). No schema enumeration.

**Severity: SEV-4 (informational, accepted).** See §Findings F1.

---

### Scenario 2 — `applied_filters` echo as a binary-search value oracle
**Walk.** The endpoint echoes `applied_filters` with the parsed (and coerced) value. If I send `?size_bytes_gte=50000000000`, the echo confirms server-side coercion succeeded. Combined with `meta.total`, this gives me a binary-search probe:

```
?size_bytes_gte=10000000000   -> total=89
?size_bytes_gte=50000000000   -> total=3
?size_bytes_gte=80000000000   -> total=1
?size_bytes_gte=100000000000  -> total=0
```

I've now learned the operator owns 89 games ≥ 10 GB, 3 games ≥ 50 GB, exactly 1 game ≥ 80 GB and ≤ 100 GB. Cross-reference public game-size data (PCGamingWiki, SteamDB): the only 80-100 GB Steam game in the operator's plausible library is, say, Call of Duty: Modern Warfare III. I've leaked one library entry via inference, without any title field being filterable.

But: **I already have the full library list via Scenario walk on TM-023 step 6.** A user with a stolen bearer can simply `GET /api/v1/games?limit=500` and read all titles directly. The oracle attack is strictly less powerful than the direct read.

So: oracle exists only if a sub-token grants size-aggregation queries but not list reads — which is not the current design (single bearer, full access). For the current single-bearer model, this oracle is dominated by the direct read.

**Severity: SEV-4** (would matter post-MVP if a read-aggregate-only token role is introduced). See §Findings F3.

---

### Scenario 3 — `meta.total` as an aggregate-count oracle
**Walk.** Same model as Scenario 2 but for the 8 status values:

```
?status=blocked          -> total=12   (operator has hidden 12 games)
?status=validation_failed -> total=4   (4 games corrupt — useful for disruption: trigger refresh)
?status=downloading      -> total=1   (real-time activity signal)
```

The `status=downloading` total at a given timestamp is a side-channel for "is the operator actively prefetching right now?" — useful for an attacker planning a DoS window. Combined with the kill-chain step 7 (prefill amplification), an attacker who notices `downloading=0` knows the operator's WAN link is currently idle and can saturate it without competing traffic.

This is a real signal but again dominated by the direct game list read (which also includes per-row `status`). Information leak is marginal beyond the existing TM-023 acknowledgement.

**Severity: SEV-4.** See §Findings F4.

---

### Scenario 4 — Two-query race between COUNT(*) and SELECT
**Walk.** `games.py:193-194`:

```python
count_row = await pool.read_one(count_sql, where_params)
rows = await pool.read_all(rows_sql, rows_params)
```

Two separate awaits. Between them, an autocommit write from the scheduler (F12 cycle inserts/updates games rows) can commit. Concrete sequence:

- t=0ms: COUNT(*) returns total=2600
- t=10ms: scheduler INSERT INTO games (... new app_id ...) commits
- t=20ms: SELECT returns 50 rows from a pool of 2601

`meta.has_more = (offset + len(games) < total)` = `0 + 50 < 2600` = True. The new row may or may not appear in the page, depending on the sort order and the inserted row's title. The total reported is stale; the row may appear or be missed on the next page.

Impact: a single eventually-consistent view. The user can refresh and get correct results. No data loss, no security boundary crossed. This is a standard read-committed snapshot consistency artifact in a non-transactional read pattern.

If the pool used a single transaction wrapping both queries (BEGIN; COUNT; SELECT; COMMIT), we'd get consistent reads. The BL4 pool doesn't expose multi-query transactions for reads, and the spec accepts eventual consistency. Phase 3 hardening could wrap both in `BEGIN DEFERRED ... COMMIT` if needed.

**Verdict: PARTIAL.** UX nit, not a security issue. See §Findings F5.

---

### Scenario 5 — JSON-bomb / billion-laughs in `metadata` column
**Walk.** `games.py:208`:

```python
parsed = json.loads(raw_meta)
metadata = parsed if isinstance(parsed, dict) else None
```

Python's `json.loads` does NOT support entity expansion (that's an XML concern; JSON has no entity references). The "JSON bomb" surface in CPython is:

1. **Deeply nested arrays/objects** — Python's recursion limit (default 1000). `json.loads("[" * 5000)` raises `RecursionError`. Caught by the `except (json.JSONDecodeError, TypeError)` clause? **NO — `RecursionError` is a `RuntimeError`, not in the except tuple.** The exception propagates up. BL5 doesn't have a global exception handler that returns a sanitized 500; FastAPI's default 500 handler returns `{"detail": "Internal Server Error"}` — no stack trace (TM-011 covered). Operator-side: a 500 reaches the client; a structured ERROR log fires with traceback (per ID3 logging chain). No data corruption.

2. **Huge but shallow string** — Python parses linearly. A 100 MB metadata blob is the harder hit. The metadata column is TEXT with no length cap in the schema. If a malicious upstream (Steam manifest) writes 100 MB into `metadata`, the parse takes seconds and consumes ~3× memory. On DXP4800 with 8 GB RAM and 10 concurrent pool connections, 10 parallel requests fetching 100 MB metadata = 3 GB transient — concerning but not crashing.

3. **Resource cap absent.** No `MAX_METADATA_BYTES` check before `json.loads`. The router pulls `raw_meta` from the row and parses unconditionally.

How does a hostile metadata value get into the column? The `metadata` column is written by the orchestrator's own data layer (F5/F6 manifest ingestion); a hostile upstream (TM-007) could ship a manifest that causes a large value to be written. The manifest-fetch path has a 128 MiB cap (DQ7) so 100 MB is in-bounds for what could end up in `metadata`.

**Severity: SEV-3.** A 100 MB metadata row + a `?status=...` filter that happens to include it = a large, slow response. RecursionError on nested-array metadata is uncaught and bubbles to a 500. See §Findings F6.

---

### Scenario 6 — Tie-breaker dedup case sensitivity (`?sort=ID:asc`)
**Walk.** `_query_helpers.py:274`:

```python
if field_name not in allow_list.fields:
    raise QueryParamError(f"{field_name!r} is not a sortable field")
```

`field_name` = "ID" (uppercase). `GAMES_SORT_ALLOW_LIST.fields = {"id","title","status","size_bytes","last_prefilled_at","last_validated_at"}` — set lookup is case-sensitive. "ID" not in fields → 400. Good: rejected, not silently treated as a different identifier. The de-dup logic is therefore never reached with a mis-cased value.

But what about a duplicate-direction attack? `?sort=id:asc,id:desc`. Both entries pass allow-list. The tie-breaker check (`any(s.field == tie_breaker.field for s in user_sort)`) sees `id` in user_sort and SKIPS appending. ORDER BY becomes `id ASC, id DESC` — SQLite uses the first only (deterministic). De-dup is preserved.

What about `?sort=id:asc,title:asc` (user explicitly sorts by id then title)? Tie-breaker = id, found in user_sort, not appended. ORDER BY = `id ASC, title ASC`. The tie-breaker on `id` is implicit. Fine.

Edge: `?sort=id` (no direction) → defaults to asc, allow-list passes, tie-breaker dedup works. Good.

**Verdict: MITIGATED.** Case-sensitive set lookup is the right behavior here.

---

### Scenario 7 — OpenAPI schema exposure
**Walk.** `/api/v1/openapi.json` is loopback-only per `LOOPBACK_ONLY_PATTERNS`. An attacker from the LAN can't reach it. But — what does it now expose to a loopback caller (a local sibling process) about BL6+BL7?

For BL7 the schema includes:
- Path: `/api/v1/games`
- Parameters: only those declared with `Query()` decorators — and the router uses `request.query_params` directly, not declared parameters. So the OpenAPI schema for BL7 has ZERO parameter documentation. The filter/sort allow-list is NOT in the rendered schema. Auditor verifies: `games.py:159-162` declares only `request: Request` and `pool: Pool`. No `Query()` params.

This is a quirk worth noting: the OpenAPI schema doesn't describe the filter/sort syntax at all. Operators discovering the API via Swagger UI will see "no params" and won't know `?platform=steam` is valid. This is documentation debt, not a security issue. From a security standpoint, the lack of schema documentation is actually a (minor, marginal) defense — the allow-list isn't exposed via OpenAPI.

Response models ARE exposed (GameResponse, GamesMeta, FilterCriterion). Field names of the response = column names of the games table. So a loopback caller learns the wire schema — but loopback is the trust boundary, and the wire schema is also visible from any successful 200 response.

**Verdict: MITIGATED.** Worth noting in docs but not a security finding.

---

### Scenario 8 — `applied_filters` echo as a stored-XSS vector for Game_shelf
**Walk.** The orchestrator returns:

```json
{"meta": {"applied_filters": {"platform": {"eq": "<script>alert(1)</script>"}}}}
```

if the attacker (with bearer) sends `?platform=<script>alert(1)</script>`. Server-side the orchestrator is fine — content-type is `application/json`, no HTML rendering happens here. But: Game_shelf (the consumer) renders `applied_filters` in its UI. If Game_shelf inserts the value into the DOM via `innerHTML` rather than `textContent`, the attacker has injected JS into the Game_shelf operator's browser session.

To exploit, the attacker needs:
1. The bearer token (TM-023 step 4-5 already grants it).
2. The operator to be browsing Game_shelf at a moment when the attacker's request's `applied_filters` echo is rendered.

The orchestrator does not return the attacker's request to the operator's browser. The attacker has to wait until the operator triggers a query whose echo happens to match — which means the attacker has to influence what the operator's browser fetches. Practical exploitation requires Game_shelf to render attacker-controlled filter values, which generally doesn't happen unless Game_shelf has its own URL-driven filter feature whose params come from user-supplied links (e.g., bookmarkable filter URLs). Plausible in TanStack-Query UIs.

This is fundamentally a Game_shelf concern. The orchestrator's responsibility is to return JSON with consistent encoding (which it does — `JSONResponse` escapes `<` `>` etc. correctly for JSON). Cross-system, flag as integration concern.

**Verdict: N/A at the orchestrator layer; flag for Game_shelf.** See §Findings F7.

---

## Findings

### SEV-1
**None.**

### SEV-2
**None.**

### SEV-3

**F6 — `metadata` JSON parse has no size cap and uncaught `RecursionError`.**
- **Location:** `src/orchestrator/api/routers/games.py:206-215`
- **Walk:** `json.loads(raw_meta)` is called on a `TEXT` column with no length cap. The `except` tuple catches `JSONDecodeError, TypeError` but NOT `RecursionError`. A deeply nested JSON array in the metadata column (5,000+ open brackets) raises `RecursionError`, which bubbles to FastAPI's default 500 handler — every `GET /api/v1/games` page including that row will 500 until the row is repaired. Large but shallow metadata (100 MB) causes a slow parse + ~3× memory transient.
- **Recommendation:** (1) Add `len(raw_meta) > MAX_METADATA_BYTES` short-circuit (suggest 1 MiB) → fall back to `metadata=None` + structured log. (2) Catch `RecursionError` in the except tuple. (3) Optionally use `json.JSONDecoder(strict=True)` with a max-depth wrapper.
- **Severity:** SEV-3 — requires a malicious or corrupt metadata row to trigger; metadata is written by the orchestrator's own data layer; LAN trust boundary mitigates external entry. But the 500 fallout is operator-impacting.

### SEV-4

**F1 — Filter allow-list enumerable via 400 messages.** Dominated by source-code visibility (open-source repo); accepted disclosure. No remediation.

**F2 — Sustained-slowdown via uncovered sort/filter columns.** BL7 allow-lists `last_validated_at` and `last_prefilled_at` filters but no schema index exists on those columns. At 1M-row scale, a hot-loop of such filtered queries will saturate the aiosqlite pool. Phase 3 hardening — add covering indexes or remove these from the filter allow-list.

**F3 — `applied_filters` echo enables a binary-search size-distribution oracle.** Dominated by the direct list read with the same bearer. Would become real if a future read-only sub-token role is introduced.

**F4 — `meta.total` per status reveals operator activity windows (notably `status=downloading`).** Dominated by the direct list read. Same future-token caveat.

**F5 — Two-query COUNT+SELECT race produces eventual-consistency artifacts.** Read-side staleness only; no security boundary crossed. UX nit. Phase 3 hardening could wrap both queries in a deferred read transaction.

**F7 — `applied_filters` echo is a potential XSS vector for downstream JSON consumers (Game_shelf).** Server-side responsibility ends at correct JSON encoding (orchestrator's `JSONResponse` is correct). Cross-system integration concern — Game_shelf must use `textContent` (or React's auto-escaping default) when rendering echoed values. Document in HANDOFF or Game_shelf integration notes.

---

## Non-findings

- **No SQL injection** — values parameter-bound, identifiers allow-list-validated. Property-test pinned.
- **No auth bypass on BL6/BL7** — neither path is in `AUTH_EXEMPT_PATHS`; substring evasion blocked by UAT-3 S2-A.
- **No CORS regression** — CORS-outermost stack inherited via `app.include_router(...)`; no per-router override.
- **No correlation_id injection** — UUIDv4 strict regex regenerates malformed input; BL6/BL7 inherit via middleware stack.
- **No oversized-body vector on GET** — handlers don't materialize bodies; 32 KiB cap enforced regardless.
- **No log-side credential leak** — neither router logs row content; `metadata_parse_failed` logs only `game_id`.
- **No stack-trace disclosure** — 503 path returns `{"detail": "database unavailable"}`; 500 path returns generic detail.
- **No OpenAPI schema leak to LAN** — `/api/v1/openapi.json` is loopback-only; furthermore the schema doesn't document the filter/sort allow-list (handler reads `request.query_params` rather than declaring `Query()` params).
- **Tie-breaker de-dup case sensitivity is correct** — `?sort=ID:asc` is rejected as not-sortable, never silently treated as a separate field.
