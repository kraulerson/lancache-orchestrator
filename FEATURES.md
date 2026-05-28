# Feature Reference

<!--
  This document is a living index of all features built during Phase 2.
  Update at Step 2.5 of every Build Loop iteration alongside the CHANGELOG and Bible.
  Purpose: Give someone a quick orientation to what the app does without reading the Bible.
  For detailed analysis, follow the links to ADRs and interface docs.
-->

## Feature 1: ID1 — SQLite Migrations Framework

**Phase Built:** 2 (Milestone B, Build Loop 1)
**Status:** Complete (2026-04-22)
**Summary:** A minimal-dependency migrations runner that applies numbered `.sql`
files to the SQLite database on container startup, with atomic apply (single
`BEGIN IMMEDIATE` / `COMMIT`), SHA-256 pinning via a `CHECKSUMS` manifest,
gap detection, post-apply schema-object sanity check, concurrent-runner
serialization, and refusal to run on network filesystems (WAL incompatible).
Migrations ship as Python package data (read-only) loaded via `importlib.resources`.
**Key Interfaces:**
  - `src/orchestrator/db/migrate.py` — runner (`run_migrations`, `MigrationError`)
  - `src/orchestrator/db/migrations/` — packaged `*.sql` files + `CHECKSUMS`
  - Env var: `ORCH_REQUIRE_LOCAL_FS=strict` (opt-in fail-closed on unknown FS)
**Related ADRs:**
  - [`ADR-0008 — Migration Runner Architecture`](ADR%20documentation/0008-migration-runner-architecture.md)
**Test Coverage:** Unit — 42 tests in `tests/db/test_migrate.py`. Regression coverage for all 8 UAT-1 findings (GH #3–#8, #12, #13) plus the 3 re-audit hardening items. Integration/E2E tests will cover this indirectly once the API layer boots migrations on startup.
**Known Limitations:**
  - Statement splitter does not honor `;` or comment delimiters inside string literals ([#19](https://github.com/kraulerson/lancache-orchestrator/issues/19)).
  - `_CREATE_TABLE_RE` misses `CREATE TEMP` / `CREATE VIRTUAL` / does not subtract on `DROP TABLE` ([#20](https://github.com/kraulerson/lancache-orchestrator/issues/20)).
  - No rollback runner (intentional for MVP); `down`-direction migrations out of scope.

---

## Feature 2: ID3 — Structured Logging with Correlation IDs

**Phase Built:** 2 (Milestone B, Build Loop 2)
**Status:** Complete (2026-04-22)
**Summary:** JSON-line structured logging via structlog, with correlation-ID
scoping (`request_context()` context manager using token-based reset that
survives nesting and exceptions), reserved-key protection (user kwargs that
collide with framework-owned keys like `correlation_id` are rescued to
`user_<key>` with numbered-slot collision handling), recursive secret
redaction (any value under a key matching the sensitive-key regex becomes
`<redacted>`; cycle-safe), and strict log-level validation (unknown values
raise `ValueError` instead of silently becoming INFO).
**Key Interfaces:**
  - `src/orchestrator/core/logging.py` — `configure_logging()`,
    `request_context()`, `new_correlation_id()`, `bind_correlation_id()`,
    `clear_request_context()`, `RESERVED_KEYS`
**Related ADRs:**
  - [`ADR-0009 — Logging Framework Architecture`](ADR%20documentation/0009-logging-framework-architecture.md)
**Test Coverage:** Unit — 55 tests in `tests/core/test_logging.py`.
Regression coverage for all 4 UAT-1 findings (GH #9, #10, #14, #15) plus
the 4 re-audit hardening items. Parametrized across 12 compound-key shapes
for redaction and 5 valid log-levels for validation. Integration coverage
will follow once the API layer boots the logger at startup.
**Known Limitations:**
  - Value-content scanning is not implemented; relies on callers using
    descriptive field names for the key-based redactor to catch secrets.
  - `_redact_sensitive_values` rebuilds dicts even when nothing redactable;
    minor perf ([#22](https://github.com/kraulerson/lancache-orchestrator/issues/22)).
  - Low-level `bind_correlation_id()` / `clear_request_context()` primitives
    are retained for unusual cases but callers should prefer
    `request_context()`.

---

## Feature 3: ID4 — Settings Module

**Phase Built:** 2 (Milestone B, Build Loop 3)
**Status:** Complete (2026-04-23)
**Summary:** Typed application configuration via pydantic-settings `BaseSettings`.
16 flat fields spanning API, database, platform sessions, Lancache cache
topology, and miscellaneous tunables. The single `SecretStr` field
(`orchestrator_token`) loads from env (`ORCH_TOKEN`) or Docker secret
(`/run/secrets/orchestrator_token`) via `AliasChoices`, whitespace-stripped
with a min-length-32 invariant. `@lru_cache` singleton accessor
(`get_settings()`) avoids import-time side effects; `reload_settings()` is the
test / SIGHUP escape hatch. A post-init `@model_validator` emits four
diagnostic WARNINGs (secret shadowed by env, non-loopback bind, wildcard
CORS, over-Spike-F chunk concurrency). Three defense-in-depth layers around
the token: `SecretStr` default censoring, `__reduce__` override blocking
pickle leaks, and `__init__` wrapper scrubbing `ValidationError.input_value`
on token-field failures.
**Key Interfaces:**
  - `src/orchestrator/core/settings.py` — `Settings`, `get_settings()`, `reload_settings()`
  - Env vars (`ORCH_` prefix, 16 total): `ORCH_TOKEN`, `ORCH_API_HOST`,
    `ORCH_API_PORT`, `ORCH_CORS_ORIGINS`, `ORCH_LOG_LEVEL`,
    `ORCH_DATABASE_PATH`, `ORCH_REQUIRE_LOCAL_FS`, `ORCH_STEAM_SESSION_PATH`,
    `ORCH_EPIC_SESSION_PATH`, `ORCH_LANCACHE_NGINX_CACHE_PATH`,
    `ORCH_CACHE_SLICE_SIZE_BYTES`, `ORCH_CACHE_LEVELS`,
    `ORCH_CHUNK_CONCURRENCY`, `ORCH_MANIFEST_SIZE_CAP_BYTES`,
    `ORCH_EPIC_REFRESH_BUFFER_SEC`, `ORCH_STEAM_UPSTREAM_SILENT_DAYS`
  - Docker secret file (alias for `ORCH_TOKEN`): `/run/secrets/orchestrator_token`
**Related ADRs:**
  - [`ADR-0010 — Settings Module Design`](ADR%20documentation/0010-settings-module-design.md)
**Test Coverage:** Unit — 67 tests in `tests/core/test_settings.py` with
100% branch coverage on `settings.py` (79 statements, 18 branches). Covers
required fields, 15 optional defaults, field validators (boundaries + enum
+ regex rejections), source precedence (init > env > .env > secrets >
default), secret-loading, 5-shape × 3-serialization redaction parametrize
(15 assertions), all 4 warnings + 1 negative case, singleton behavior, and
2 SEV-2 regression tests (pickle-block, ValidationError scrubbing).
Integration coverage will follow once BL4 DB pool and BL5 FastAPI layer
consume `get_settings()` at startup.
**Known Limitations:**
  - ID1's migration runner still reads `ORCH_REQUIRE_LOCAL_FS` directly
    via `os.environ.get()` rather than `get_settings().require_local_fs`.
    Tracked as SEV-4 follow-up ([BL3-ID1-rewire]).
  - Only `orchestrator_token` is `SecretStr`. When F1/F2 platform auth
    adds a second `SecretStr` field, the 3 redaction tests will be
    promoted to parameterize over every declared `SecretStr` field.
    Tracked as SEV-4 follow-up ([BL3-redaction-introspection]).
  - No CLI `config show` command yet (Bible §9 says the CLI will have one).
    Deferred to BL-later once the CLI is wired.
  - pydantic-settings emits `UserWarning: directory "/run/secrets" does
    not exist` ~60×/suite-run when tests construct `Settings` without
    overriding `secrets_dir`. Candidate for a `filterwarnings` entry in
    pyproject.toml ([BL3-warnings-filter]).

---

## Feature 4: BL4 — Async DB Pool

**Phase Built:** 2 (Milestone B, Build Loop 4)
**Status:** Complete (2026-04-25)
**Summary:** Async DB pool on top of `aiosqlite` with hybrid topology
(1 dedicated writer connection + N reader connections, default 8).
Defense-in-depth write serialization (`asyncio.Lock` +
`BEGIN IMMEDIATE` + `busy_timeout=5000`). Comprehensive API surface:
single-statement helpers (`read_one`, `read_all`, `read_one_as`,
`read_all_as`, `read_stream`, `execute_write`, `execute_many_write`),
multi-statement transaction contexts (`read_transaction`,
`write_transaction`) returning typed `ReadTx`/`WriteTx` handles, raw
connection escape hatches (`acquire_reader`, `acquire_writer`). 11-class
exception hierarchy with `sqlite_errorcode`-based integrity
classification (unique / fk / notnull / check / primarykey). 13 stable
structured-event names (`pool.initialized`, `pool.connection_lost`,
`pool.connection_replaced`, `pool.replacement_storm`, etc.). Connection
replacement state machine on disk-I/O errors, with per-role storm guard
at >3 replacements in 60 s. Module-level singleton (`init_pool`,
`get_pool`, `reload_pool`, `close_pool` with 30 s hard timeout).
`pool.schema_status()` introspection surface for `/api/v1/health`.
**Key Interfaces:**
  - `src/orchestrator/db/pool.py` — `Pool`, `ReadTx`, `WriteTx`,
    11 exception classes, module singleton
  - `src/orchestrator/db/migrate.py` — `verify_schema_current()` helper
    called by `Pool.create()` for schema-drift detection
  - Env vars (5 new, `ORCH_` prefix): `ORCH_POOL_READERS`,
    `ORCH_POOL_BUSY_TIMEOUT_MS`, `ORCH_DB_CACHE_SIZE_KIB`,
    `ORCH_DB_MMAP_SIZE_BYTES`, `ORCH_DB_JOURNAL_SIZE_LIMIT_BYTES`
**Memory baseline:** `(pool_readers + 1) × db_cache_size_kib +
db_mmap_size_bytes`. Default config (8 readers, 16 MiB cache, 256 MiB
mmap) ≈ 400 MiB resident. Operators on memory-constrained hardware
should tune `pool_readers` and `db_cache_size_kib` together.
**Related ADRs:**
  - [`ADR-0011 — DB Pool Architecture`](ADR%20documentation/0011-db-pool-architecture.md)
  - [`ADR-0010 — Settings Module Design`](ADR%20documentation/0010-settings-module-design.md)
    (BL4 addendum — 5 new fields)
**Test Coverage:** Unit + property + chaos — 117 tests across 5 files
in `tests/db/` plus 3 `@pytest.mark.slow` integration tests deferred from
default runs. 81% branch coverage on `pool.py` (594 stmts, 114 branches);
remaining gaps are error-path catch-alls + unused
`ReaderUnreachableError`/`WriterUnreachableError` exception classes,
filed as a follow-up.
**Known Limitations:**
  - 81 % branch coverage (plan target was 100 %); follow-up issue.
  - `@pytest.mark.slow` tests not run in default CI; deferred to nightly.
  - `ReaderUnreachableError`/`WriterUnreachableError` exception classes
    are defined but unused — anticipated for a future
    `health_check_failed` policy that escalates beyond `pool.health_check_partial`.

---

## Feature 5: BL5 — FastAPI Skeleton

**Phase Built:** 2 (Milestone B, Build Loop 5)
**Status:** Complete (2026-04-27)
**Summary:** FastAPI application factory at `src/orchestrator/api/main.py:create_app`
producing a configured FastAPI app with a 4-layer pure-ASGI middleware
stack (CorrelationId / BodySizeCap / BearerAuth / CORS), an
`@asynccontextmanager` lifespan that runs migrations and initializes
the BL4 pool singleton, and one router (`/api/v1/health`) returning
the 7-field response per Bible §8.4. Auth runs as middleware (not
`Depends`) for TM-013 fingerprinting defense — 404s on non-exempt
paths require auth. OQ2 loopback enforcement (`POST /api/v1/platforms/{name}/auth`
requires `client.host == 127.0.0.1`) is in place even though the
handler doesn't ship until F1/F2. Body-size cap (32 KiB) enforced via
streaming-aware middleware: Content-Length proactive check + chunked
`receive()` interception. Bearer auth uses `hmac.compare_digest` on
UTF-8-encoded bytes (timing-safe). `bearerAuth` security_scheme
registered so Swagger UI's Authorize button works.
**Key Interfaces:**
  - `src/orchestrator/api/main.py` — `create_app()` factory + lifespan
    + middleware registration + OpenAPI security scheme
  - `src/orchestrator/api/dependencies.py` — `AUTH_EXEMPT_PREFIXES`,
    `LOOPBACK_ONLY_PATTERNS`, `BODY_SIZE_CAP_BYTES`, `__version__`,
    `get_pool_dep`
  - `src/orchestrator/api/middleware.py` — `CorrelationIdMiddleware`,
    `BodySizeCapMiddleware`, `BearerAuthMiddleware`
  - `src/orchestrator/api/routers/health.py` — `GET /api/v1/health`,
    `HealthResponse` Pydantic model with `extra="forbid"`
  - Boot: `uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765`
**BL5 ship state — `/health` returns 503 by-design.** Three of the
seven subsystems (`scheduler_running`, `lancache_reachable`,
`validator_healthy`) are stub-false until BL6+ ships them as features
land. Body still carries the full 7-field response so the operator
sees exactly which subsystem is unhealthy. Container HEALTHCHECK +
k8s liveness probes should expect 503 during this transition.
**Related ADRs:**
  - [`ADR-0012 — FastAPI Skeleton Architecture`](ADR%20documentation/0012-fastapi-skeleton-architecture.md)
  - [`ADR-0010 — Settings Module Design`](ADR%20documentation/0010-settings-module-design.md) (consumed)
  - [`ADR-0011 — DB Pool Architecture`](ADR%20documentation/0011-db-pool-architecture.md) (consumed)
**Test Coverage:** Unit + integration — 48 tests across 7 files in
`tests/api/`. 96% branch coverage on `src/orchestrator/api/` (236 stmts /
34 branches). Two app fixtures: `unit_app` (no lifespan, deps
overridden, app.state stubbed — 95% of tests) + `lifespan_app` (real
lifespan via `asgi-lifespan.LifespanManager` — for integration
coverage). Three client fixtures (default / loopback-127.0.0.1 /
external-IP) for OQ2 testing via `httpx.ASGITransport(app, client=("ip", port))`.
**Known Limitations:**
  - `/api/v1/health` returns 503 by-design until BL6+ flips
    `scheduler_running` / `lancache_reachable` / `validator_healthy`.
  - Streaming body-cap (chunked transfer-encoding) is best-effort for
    *unauthenticated* requests — bearer-auth rejects before the body
    is read, so the streaming cap doesn't fire. Content-Length
    proactive path covers all requests; streaming path covers
    authenticated requests that consume the body. Verified by direct
    middleware unit test in BL5; full HTTP-level streaming integration
    coverage lands when the first body-consuming endpoint ships in
    BL6+.
  - 4 percentage points coverage gap (96% vs. plan target 95%
    achieved — no follow-up needed; the 4% missing is defensive
    catch blocks for `SchemaNotMigratedError`/`SchemaUnknownMigrationError`/`PoolError`
    in lifespan + a non-numeric Content-Length defensive parse).

---

## Feature 6: BL6 — `GET /api/v1/platforms` (read-only)

**Phase Built:** 2 (Milestone B, Build Loop 6)
**Status:** Complete (2026-05-04)
**Summary:** First real F9 read endpoint on the BL5 substrate. Returns
the auth + sync status of every configured platform (always exactly
two rows: `steam`, `epic`). Bearer-required (NOT in
`AUTH_EXEMPT_PATHS`). Wrapped envelope shape `{"platforms": [...]}` —
locks the convention every future F9 endpoint inherits. Six fields
per platform; `config` excluded entirely. `last_error` truncated to
200 chars at the API layer (defense-in-depth on top of upstream
redaction). Pool failures translate to HTTP 503 with structured
`api.platforms.read_failed` log event. Steam-first sort via SQL
`CASE WHEN name = 'steam' THEN 0 ELSE 1 END, name`.
**Key Interfaces:**
  - `src/orchestrator/api/routers/platforms.py` —
    `PlatformResponse` + `PlatformListResponse` Pydantic models with
    `extra="forbid"`, `list_platforms` GET handler
  - Wired in `src/orchestrator/api/main.py` via
    `app.include_router(platforms_router)`
  - Wire format: `{"platforms": [{"name", "auth_status",
    "auth_method", "auth_expires_at", "last_sync_at", "last_error"},
    ...]}`
**Locked decisions (D1-D8):**
  D1 `config` excluded · D2 wrapped envelope · D3 `last_error` 200-char
  truncation · D4 Steam-first sort · D5 no ETag for v1 · D6 PoolError
  → 503 · D7 bearer required · D8 `extra="forbid"`. See
  [spec](superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md).
**Test Coverage:** 23 tests in `tests/api/test_platforms_router.py`
(~280 LoC) across 7 classes: happy path, auth, last_error truncation,
config exclusion, response schema strictness, pool-failure 503,
ordering. ≥95% branch coverage on `routers/platforms.py`.
**Related Audit:** [`bl6-f9-platforms-readonly-security-audit.md`](security-audits/bl6-f9-platforms-readonly-security-audit.md) — 0 findings.
**Known Limitations:**
  - No ETag / conditional-response support (D5, deferred to a
    follow-up if Game_shelf surfaces real polling load).
  - No per-platform endpoint (`GET /api/v1/platforms/{name}`); the
    list is small enough that clients fetch the whole thing.
  - No `meta` envelope field yet; will be added uniformly when the
    first paginated F9 endpoint (`/games`) lands.

---

## Feature 7: BL7 — `GET /api/v1/games` (read-only, paginated)

**Phase Built:** 2 (Milestone B, Build Loop 7)
**Status:** Complete (2026-05-20)
**Summary:** First paginated F9 read endpoint on the BL5+BL6 substrate.
Returns the games library with filter, sort, and offset-based
pagination. Wrapped envelope `{"games": [...], "meta": {...}}` with
rich meta including `total`, `has_more`, `applied_filters`, and
`applied_sort` echo. Per-endpoint filter/sort allow-list acts as both
the security boundary AND the docs surface.
**Key Interfaces:**
  - `src/orchestrator/api/routers/games.py` — `GameResponse`,
    `GameListResponse`, `GamesMeta`, `FilterCriterion`,
    `SortFieldResponse` Pydantic models; `list_games` handler
  - `src/orchestrator/api/_query_helpers.py` — `parse_pagination`,
    `parse_filters`, `parse_sort`, `build_where_clause`,
    `build_order_by_clause`; `FilterAllowList`, `SortAllowList`,
    `FilterFieldSpec`, `SortField`, `PaginationParams`,
    `QueryParamError`
  - Wired in `src/orchestrator/api/main.py` via
    `app.include_router(games_router)`
**Locked decisions (D1-D12):** offset pagination · rich meta envelope ·
default=50/max=500 (reject 400) · operator-suffix filters · multi-field
sort with `id:asc` tie-breaker (de-dup) · metadata as parsed JSON ·
last_error 200-char truncation · empty result returns 200 · unknown
field/op → 400 · Pydantic `extra="forbid"` · bearer required ·
PoolError → 503. See
[spec](superpowers/specs/2026-05-17-bl7-games-readonly-design.md).
**Test Coverage:** 70 tests across `tests/api/test_games_router.py`
(38 HTTP-level tests) and `tests/api/test_query_helpers.py` (32 unit
tests including a Hypothesis property test for SQL injection
resistance). Branch coverage ≥95% on both modules.
**Related Audit:** [`bl7-f9-games-readonly-security-audit.md`](security-audits/bl7-f9-games-readonly-security-audit.md) — 0 findings.
**Known Limitations:**
  - No title search (`_like`) in BL7 — deferred to BL-future-search
    (needs FTS5 or trigram support). Game_shelf can client-side filter
    50 rows on title trivially.
  - No per-game endpoint `GET /api/v1/games/{id}` — clients read the
    list and index client-side; if a real need surfaces, additive.
  - `_query_helpers.py` operator surface declares `gt`/`lt`/`ne` for
    future endpoints, but no current field's allow-list permits them.
    They become available when a future endpoint opts in.

---

## Feature 8: BL8 — `GET /api/v1/jobs` (read-only, paginated)

**Phase Built:** 2 (Milestone B, Build Loop 8)
**Status:** Complete (2026-05-20)
**Summary:** Second paginated F9 read endpoint. Returns the orchestrator
jobs feed with filter, sort, and offset-based pagination. Inherits
BL7's wrapped envelope `{"jobs": [...], "meta": {...}}` with all
UAT-4 hardening (compact applied_filters echo, INT64 range checks,
_in cardinality cap, identifier validation, etc.). Default sort
`id:desc` keeps active jobs surfaced via explicit
`?state_in=queued,running` filter (the canonical Game_shelf
active-jobs query). **Zero changes to the shared `_query_helpers.py`
module** — validates the convention-propagation proposition.
**Key Interfaces:**
  - `src/orchestrator/api/routers/jobs.py` — `JobResponse`,
    `JobListResponse`, `JobsMeta`, `SortFieldResponse` Pydantic models;
    `JOBS_FILTER_ALLOW_LIST`, `JOBS_SORT_ALLOW_LIST`; `list_jobs` handler
  - Wired in `src/orchestrator/api/main.py` via
    `app.include_router(jobs_router)`
**Locked decisions (D1-D14):** id:desc default sort · payload as parsed
JSON · _is_null deferred · no derived fields · error 200-char truncation
· D6-D14 inherited from BL7+UAT-4. See
[spec](superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md).
**Test Coverage:** 37 tests in `tests/api/test_jobs_router.py` across
9 classes (empty DB, happy path, pagination, enum filters, scalar
filters, timestamp filters, sort + tie-breaker dedup, applied echo,
payload + error handling, error paths, pool failure). Plus
`jobs_pool_seeded` fixture in `conftest.py` (~50 jobs across all enum
combinations, including 1 oversized + 1 malformed + 1 non-dict payload).
**Related Audit:** [`bl8-f9-jobs-readonly-security-audit.md`](security-audits/bl8-f9-jobs-readonly-security-audit.md) — 0 findings.
**Known Limitations:**
  - No `_is_null` operator — orphan-job queries (`?game_id_is_null=true`)
    require direct DB access until a real Game_shelf need surfaces.
  - No derived fields — `duration_sec` is client-derivable from
    `started_at` + `finished_at`; `age_sec` would break response
    determinism.
  - `?source=...` queries are full-table-scan (no index). At expected
    scale (thousands of rows) this is acceptable; `idx_jobs_source`
    is a future migration if it becomes a hot path.

---

## Feature 9: BL9 — `GET /api/v1/manifests` (read-only, paginated)

**Phase Built:** 2 (Milestone B, Build Loop 9)
**Status:** Complete (2026-05-20)
**Summary:** Third paginated F9 read endpoint. Introduces the **`?include=`
opt-in expansion convention** via a thin (~30 LoC) extension to
`_query_helpers.py`. Default sort `fetched_at:desc` matches the
`idx_manifests_game_fetched` index. `raw` BLOB column excluded. With
`?include=game`, response embeds `{title, platform, app_id}` via a
follow-up `WHERE id IN (...)` games lookup keyed by the distinct
game_ids on the page (switched from the LEFT JOIN spec'd in D7 to
avoid an ambiguous-`id` issue with the unqualified ORDER BY
tie-breaker — same wire behavior, cleaner SQL). Validates that the
shared module can grow new primitives cheaply when a real new use case
(FK expansion) arises.
**Key Interfaces:**
  - `src/orchestrator/api/routers/manifests.py` — `ManifestResponse`,
    `ManifestListResponse`, `ManifestsMeta`, `SortFieldResponse`,
    `GameSummary` Pydantic models; 3 allow-lists;
    `list_manifests` handler with separate games-lookup query
  - `src/orchestrator/api/_query_helpers.py` — NEW: `IncludeAllowList`
    dataclass + `parse_includes` function; `"include"` added to
    `_RESERVED_PARAM_NAMES`
  - Wired in `src/orchestrator/api/main.py` via
    `app.include_router(manifests_router)`
**Locked decisions (D1-D8):** raw BLOB excluded · default sort
fetched_at:desc · version eq+_in · ?include=game always-present field
null when absent · NEW IncludeAllowList primitive · GameSummary 3-field
shape · separate follow-up games query (originally spec'd as LEFT JOIN;
swapped during implementation — same wire) · applied_includes sorted
echo. D9-D20 inherited from BL7+UAT-4+BL8. See
[spec](superpowers/specs/2026-05-20-bl9-manifests-readonly-design.md).
**Test Coverage:** 34 tests in `tests/api/test_manifests_router.py`
across 10 classes + 8 tests in `tests/api/test_query_helpers.py` for
the new `parse_includes` + `IncludeAllowList` primitives. Plus
`manifests_pool_seeded` fixture in `conftest.py` (21 manifests across
5 baseline games).
**Related Audit:** [`bl9-f9-manifests-readonly-security-audit.md`](security-audits/bl9-f9-manifests-readonly-security-audit.md) — 0 findings.
**Known Limitations:**
  - `raw` BLOB column not exposed; out-of-band diagnostic endpoint
    (`GET /manifests/{id}/raw`) deferred until real operator need
    surfaces.
  - `?sort=total_bytes:desc` / `?sort=chunk_count:desc` use full table
    scan + temp B-tree (no covering index). Acceptable at expected
    scale (thousands of manifests); add covering index only if
    profiling shows a hot path.
  - `?include=game` adds one extra round-trip when requested. At expected
    scale (one IN-list of at most `limit` keys) the cost is negligible;
    JOIN-based single-query alternative was considered but caused
    ambiguous-`id` SQL builder conflict (see security audit).

---

## Feature 10: BL10 — Steam authentication substrate (F1 milestone 1/3)

**Phase Built:** 2 (Milestone B, Build Loop 10)
**Status:** Complete (2026-05-24)

**Summary:** First BL of the F1 (Steam credentials + fetcher) milestone.
Subprocess-isolated steam-next worker + IPC contract + two-step auth
API + session persistence + `platforms` table integration. Operator can
authenticate to Steam (including 2FA) via the orchestrator's HTTP API;
session persists across container restarts.

**Key Interfaces:**
  - `src/orchestrator/platform/steam/client.py` — `SteamWorkerClient`
    (asyncio-side lifecycle + IPC + correlation)
  - `src/orchestrator/platform/steam/worker.py` — subprocess entrypoint
    (gevent-patched; runs steam-next)
  - `src/orchestrator/platform/steam/protocol.py` — typed message envelopes
  - `src/orchestrator/platform/steam/session.py` — atomic metadata file
  - `src/orchestrator/api/routers/auth.py` — 3 endpoints

**Locked decisions (D1-D20 of F1 spec; BL10-relevant subset):**
  - D1 subprocess worker · D2 newline-delimited JSON · D3 two-step auth
    with challenge_id · D4 steam-next dir + orchestrator metadata file
  - D11 5-min in-memory challenge TTL · D12 NO tokens in platforms.config
  - D13 dual-venv container · D17 max 3 restart attempts · D20 10 MiB IPC
    line cap

**Test Coverage:** 43 new tests across:
  - `tests/platform/steam/test_protocol.py` (8) — envelope encode/decode
  - `tests/platform/steam/test_client_unit.py` (5) — msg_id correlation,
    error paths, restart-storm guard
  - `tests/integration/test_steam_client_subprocess.py` (5) — real
    subprocess IPC plumbing against mock worker
  - `tests/platform/steam/test_session.py` (5) — atomic write, 0600,
    sha256 prefix correctness
  - `tests/api/test_auth_router.py` (14) — endpoint contracts +
    loopback enforcement + DB updates + no-secret-in-logs
  - `tests/core/test_settings.py` (+3) — new Settings field validation
  - `tests/api/test_middleware_bearer_auth.py` (+3) — loopback regex
    extension

**Related Audit:** `docs/security-audits/bl10-f1-steam-auth-substrate-security-audit.md` — 0 findings.

**Known Limitations:**
  - Live Steam-side validation deferred to UAT-6 (manual session,
    operator's real account).
  - Library enumeration + manifest fetching land in BL11 / BL12.
  - No CLI subcommand (`orchestrator-cli auth steam`) — F11 is post-MVP;
    operator uses curl + bearer.

---

## Feature 11: BL11 — Steam Library Sync (F1 milestone 2/3)

**Phase Built:** 2 (Milestone B, Build Loop 11)
**Status:** Complete (2026-05-25)

**Summary:** Second BL of the F1 milestone. Operationalizes Steam library
enumeration end-to-end: a generic single-loop asyncio jobs dispatcher in
the orchestrator process, a `library_sync` handler that calls the
steam-worker subprocess's new `library.enumerate` IPC op and upserts the
operator's owned Steam apps into the `games` table. A
`POST /api/v1/platforms/steam/library/sync` endpoint provides manual
operator-driven re-sync with handler-side dedup of in-flight jobs; both
Steam auth-success paths auto-queue a `library_sync` job (best-effort).

**Key Interfaces:**
  - `src/orchestrator/jobs/__init__.py` — package marker
  - `src/orchestrator/jobs/worker.py` — `Deps` dataclass, `claim_next_job`,
    `mark_succeeded`, `mark_failed`, `worker_loop` (single-loop dispatcher)
  - `src/orchestrator/jobs/handlers/__init__.py` — `HANDLERS` registry
    with auto-registered built-ins
  - `src/orchestrator/jobs/handlers/library_sync.py` — `library_sync_handler`
  - `src/orchestrator/api/routers/sync.py` — manual sync endpoint
  - `src/orchestrator/platform/steam/worker.py` — `_handle_library_enumerate`
  - `src/orchestrator/platform/steam/client.py` — `library_enumerate()` method
  - `src/orchestrator/api/main.py` — lifespan now spawns + cleanly stops
    the jobs worker asyncio task (5 s shutdown timeout)

**Locked decisions (spec §5 + plan):**
  - D6 jobs-based async manifest fetching (jobs dispatcher landed here,
    used by BL12) · D7 auto-trigger on auth + manual endpoint
  - D10 single-worker job loop · P2 SELECT-then-UPDATE inside
    `write_transaction()` for atomic claim · P8 handler-side dedup
    (race-tolerant: idempotent UPSERT) · P9 auth auto-trigger is
    best-effort · P11 single-row UPSERT (not bulk executemany)
  - P12 `metadata` JSON shape: `{"depots": [int, ...], "steam_packages": []}`

**Test Coverage:** ~39 new tests:
  - `tests/jobs/test_worker.py` (16) — claim atomicity, mark helpers,
    dispatch, unknown-kind handling, handler-crash isolation, prompt
    shutdown
  - `tests/jobs/test_library_sync_handler.py` (13) — upsert happy paths,
    idempotency, preservation of downstream lifecycle columns, error
    propagation (non-steam platform, missing steam_client, IPCTimeout,
    SteamWorkerError)
  - `tests/api/test_sync_router.py` (7) — queue + dedup + auth + 503 path
  - `tests/api/test_auth_router.py` (+3) — auto-trigger on no-2FA and
    2FA paths; queue failure is swallowed without failing auth
  - `tests/platform/steam/test_client_unit.py` (+2) — `library_enumerate`
    IPC round-trip + NotAuthenticated error mapping

**Related ADRs:**
  - ADR-0013 — Steam-next subprocess isolation (inherited; no new ADR for
    BL11 — single-loop jobs design and SELECT-then-UPDATE claim are
    routine FastAPI/SQLite patterns)

**Known Limitations:**
  - Live Steam-side enumeration (real `get_product_info` interaction)
    deferred to UAT-6 — assistant cannot drive interactive Steam login.
  - Concurrent multi-job dispatch deferred per D10 — single asyncio
    loop processes one job at a time. Adequate for F1's manual + auth-
    triggered cadence; revisit when F12 ships scheduled sync.
  - Race window between dedup SELECT and INSERT can produce one extra
    `queued` library_sync row on concurrent auth + manual POST. Accepted
    per P8 — the handler is idempotent, so the second worker pass
    no-ops at the UPSERT layer.
  - Manifest fetching (BL12) is the F1 milestone 3/3 and lands after UAT-6.

---

## Feature 12: ID2 — Lancache Self-Test

**Phase Built:** 2 (Milestone B, Build Loop 12)
**Status:** Complete (2026-05-27)

**Summary:** Implements the ID2 implicit-dependency: a self-test that
exposes `lancache_reachable` on `/api/v1/health` derived from a real
HTTP probe of the lancache `lancache-heartbeat` endpoint. Replaces the
BL5 hardcoded `False`. Probe is async, time-bounded (5s timeout
default), cache-TTL'd (30s default) so /health requests don't hammer
lancache, and concurrency-safe (asyncio.Lock collapses parallel
callers onto a single in-flight HTTP request).

**Key Interfaces:**
  - `src/orchestrator/lancache/__init__.py` — new package marker
  - `src/orchestrator/lancache/heartbeat.py` — `LancacheProbe` class
    (`.probe()`, `.last_result()`, `.last_checked_at_mono()`, `.invalidate()`)
  - `src/orchestrator/api/main.py` lifespan — constructs the probe on
    boot, stashes on `app.state.lancache_probe`
  - `src/orchestrator/api/routers/health.py` — `await probe.probe()`
    per `/health` request; falls back to `False` if probe is absent
    (e.g., no-lifespan test fixtures)
  - Settings: `lancache_heartbeat_url`, `lancache_probe_timeout_sec`,
    `lancache_probe_cache_ttl_sec`

**Locked design decisions:**
  - **Cache TTL pattern, not polling.** /health is a low-volume endpoint
    (operator-driven + container HEALTHCHECK every 30s); avoid a
    background polling task whose lifecycle has to track FastAPI's.
  - **All failure modes → False.** Connect timeout, read timeout,
    connect error, non-200, even unexpected exceptions — they all
    surface as `lancache_reachable: false`. /health only needs the
    boolean, and the structured log emits the failure cause.
  - **No DI override of `app.state.lancache_probe` in tests.** The
    no-lifespan `unit_app` fixture relies on the `getattr(..., None)`
    fallback in the router; explicit overrides happen via direct
    `app.state.lancache_probe = stub` assignment (4 tests in
    `test_health_endpoint.py`).

**Test Coverage:** 16 new tests in `tests/lancache/test_heartbeat.py`
(initial state, success path, every documented httpx error, TTL cache
hit/miss/refresh/invalidate, concurrent-probe collapse,
last_checked_at advancement, URL validation). 4 new tests in
`tests/api/test_health_endpoint.py` (probe-absent fallback,
probe-up wiring, probe-down wiring, removal of the now-stale
"3 stubbed subsystems" assertion). 7 new tests in
`tests/core/test_settings.py` for the 3 new fields' defaults +
boundaries. Full suite: 790 pass.

**Related ADRs:**
  - None new — design lives in FRD ID2 + `docs/phase-1/architecture-proposal.md`

**Known Limitations:**
  - `/health` still returns 503 because scheduler_running + validator_healthy
    remain stub-false until those subsystems ship. ID2 alone doesn't flip
    /health to 200; it makes one of the three "real" instead of stubbed.
  - The probe's TTL means lancache-down detection latency is up to 30s
    after the failure starts. Operators who want faster detection can
    set `ORCH_LANCACHE_PROBE_CACHE_TTL_SEC=5` (or 0 for no caching).

---

## Feature 13: ID6 — Startup Job Reaper

**Phase Built:** 2 (Milestone B, Build Loop 13)
**Status:** Complete (2026-05-27)

**Summary:** FastAPI lifespan startup now marks every job in
`state='running'` as `failed` BEFORE the BL11 jobs worker spawns. The
previous worker (in the prior orchestrator process) died with its
container; those rows are orphaned and would otherwise sit `running`
forever, blocking the manual-sync dedup check and confusing
operators. Atomic single-`UPDATE` reap; idempotent; defensive
try/except around the call so a database hiccup at boot doesn't
abort startup.

**Key Interfaces:**
  - `src/orchestrator/jobs/reaper.py` — `reap_running_jobs(pool) -> int`
  - `src/orchestrator/api/main.py` lifespan step 2b — invokes the reaper
    after `init_pool()` and before the steam worker / jobs worker spawn

**Design decisions:**
  - **Reap-all-on-boot** (not "reap stale by started_at age"). The
    orchestrator owns the worker process; if the orchestrator process
    is starting, no other process can be holding a legitimate
    `running` job. Single-orchestrator deployment is locked per
    PROJECT_BIBLE §3.1 / Intake §6.4 — multi-orchestrator concurrency
    is explicitly out of scope.
  - **Boot continues on reaper failure.** A database hiccup during
    the reap shouldn't prevent the orchestrator from serving /health
    or accepting auth requests. The error is logged at ERROR; orphans
    remain `running` until the next successful boot.

**Test Coverage:** 10 new tests:
  - 7 unit (empty, no-running, single, multiple, mixed-states,
    idempotency, error-message-length-contract)
  - 3 integration via `asgi_lifespan.LifespanManager` (orphan reaped
    on boot, terminal states untouched, empty table boots cleanly)

**Related ADRs:**
  - None new — design follows FRD §5 ID6 directly.

**Known Limitations:**
  - The reaper marks ALL `running` jobs failed, even if a developer
    is intentionally testing a long-running flow that survives a
    re-create. Use `--reload`-aware development setups or temporarily
    move the job to a non-running state before restart.
  - No "reaped-at" timestamp — failed jobs from the reaper are
    indistinguishable from failed jobs from handler exceptions
    except via the `error` column substring match. Acceptable for
    MVP; revisit when telemetry needs grow.

---

## Feature 14: F12 — Scheduler subsystem

**Phase Built:** 2 (Milestone B, Build Loop 14)
**Status:** Complete (2026-05-28)

**Summary:** Periodically enqueues `library_sync` jobs via APScheduler
3.11.2 `AsyncIOScheduler` integrated into FastAPI lifespan. Replaces
the BL5 stub-false `/health.scheduler_running` with a real boolean.
Decoupled architecture: the scheduler only stamps queued `jobs` rows
(via thin async callbacks) — the BL11 jobs worker actually executes
handlers. Means a slow library_sync can never block the scheduler.

**Key Interfaces:**
  - `src/orchestrator/scheduler/manager.py` — `SchedulerManager` facade
    with idempotent `.start()` / `.shutdown()` and a `.running` property
  - `src/orchestrator/scheduler/jobs.py` — `enqueue_library_sync(pool)`
    cron callback (dedup-aware; never raises)
  - `src/orchestrator/api/main.py` lifespan step 4b — boots the manager
    after the jobs worker spawns; shuts it down FIRST so no new work
    is enqueued during teardown
  - `src/orchestrator/api/routers/health.py` — `getattr(app.state,
    "scheduler_manager", None)` + `.running` drives the bool
  - Settings: `scheduler_enabled`, `scheduler_library_sync_interval_sec`

**Locked design decisions** (see
[`docs/superpowers/specs/2026-05-28-f12-scheduler-design.md`](superpowers/specs/2026-05-28-f12-scheduler-design.md)):
  - **D1 AsyncIOScheduler** (v3.11.2 pinned, matches asyncio loop)
  - **D2 In-memory MemoryJobStore** (cron config re-renders on boot;
    no persistence contention with the BL4 pool)
  - **D4 max_instances=1** per scheduled job (no pile-up)
  - **D5 misfire_grace_time=None** (always fire on next opportunity)
  - **D6 Scheduler enqueues, jobs worker executes** (decoupled; slow
    handler can't block scheduler ticks)
  - **D7 Dedup at enqueue time** (mirrors the manual sync endpoint)
  - **D11 Scheduler start failure does NOT crash boot** (logged at
    CRITICAL; `/health` returns 503 via JQ3)

**Test Coverage:** 25 new tests across 5 files:
  - `tests/scheduler/test_jobs.py` (7): enqueue happy path, queued/
    running dedup, terminal-states ignored, kind+platform isolation,
    pool error swallow
  - `tests/scheduler/test_manager.py` (7): enabled/disabled boot,
    job registration, interval pass-through, idempotent start/stop
  - `tests/api/test_lifespan_scheduler.py` (3): integration —
    scheduler starts in lifespan, disabled-via-settings, /health
    reflects running=True
  - `tests/api/test_health_endpoint.py` (+3): probe-absent fallback,
    running-true, running-false wiring
  - `tests/core/test_settings.py` (+5): defaults + boundaries for
    the 2 new fields

**Related ADRs:** None new (APScheduler choice locked in PROJECT_BIBLE §3.1).

**Known Limitations:**
  - The scheduled library_sync fails with `NotAuthenticated` after a
    container restart (per documented limitation #108 / `docs/known-
    limitations.md`) until the operator re-auths. F13 will need its
    own auth check, or this will be revisited when Steam library
    evaluation (#111) ships an alternative session-persistence path.
  - /health still returns 503 because `validator_healthy` remains
    stub-false until F7 validator ships. F12 alone doesn't flip
    /health to 200 — but it's one of the three required.
  - No persistent job store. If the orchestrator is down for >6h,
    the next library_sync fires at boot+interval, not at boot. F12
    `coalesce=True` ensures multiple missed fires collapse to one.

---

## Feature 15: F10 — Status Page

**Phase Built:** 2 (Milestone B, Build Loop 15)
**Status:** Complete (2026-05-28)

**Summary:** Single-file HTML status dashboard at `GET /`. Operator-
facing summary of system state — Health, Platforms, Active Jobs,
Library Stats, Recent Errors — polled from existing `/api/v1/*`
endpoints. Self-contained (no external CSS/JS), works offline on
LAN-only deployments.

**Key Interfaces:**
  - `src/orchestrator/api/routers/status.py` — `GET /` returns the
    embedded HTML+CSS+JS as a single text/html response
  - `src/orchestrator/api/dependencies.py` — adds `("/", False)` to
    `AUTH_EXEMPT_PATHS` so the page itself bypasses bearer auth (JS
    inside the page handles token prompting + storage)
  - Endpoints the JS polls: `/api/v1/health`, `/api/v1/platforms`,
    `/api/v1/jobs?state=queued|running|failed`, `/api/v1/games?limit=1`,
    `/api/v1/manifests?limit=1`

**Locked design decisions (Bible §9.3):**
  - **Single HTML file** — no separate CSS/JS assets, no Jinja templates,
    no JS framework. Bundle is ~14 KB raw, ~5.8 KB gzipped (well
    under the < 20 KB ceiling).
  - **Bearer in sessionStorage + `prompt()`** — token is held only
    for the browser tab's lifetime; closes-tab-loses-token by design.
    FG1 tracks post-MVP HTML-form replacement.
  - **Color + icon + text label** for every status indicator
    (Intake §9 colorblind-safe). Text labels: OK / DEGRADED / ERROR /
    UNKNOWN / IDLE / NONE / N ACTIVE / N FAILED.
  - **Polling cadence** — fast tier (2 s): health, platforms, active
    jobs. Slow tier (10 s): library stats, recent errors. Backoff to
    10 s on 5xx until success returns.
  - **Security headers** — `Cache-Control: no-store`,
    `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
    `Referrer-Policy: no-referrer`, `<meta name="robots" content="noindex,nofollow">`.
  - **No external dependencies** — explicit regex test rejects any
    `<script src="http..."/>` or external stylesheet. Operator's
    lancache LAN may not have internet.

**Test Coverage:** 18 new tests in `tests/api/test_status_router.py`:
  - Route returns 200 + text/html
  - Bearer auth exemption
  - 5 panel IDs present
  - 5 pill IDs present
  - Accessibility text labels visible without JS
  - Size < 60 KB raw / < 20 KB gzipped
  - No external script/stylesheet sources
  - All 5 referenced API endpoints present (drift detection)

**Related ADRs:** None new — design lives in PROJECT_BIBLE §9.3.

**Known Limitations:**
  - **No CSRF protection on `prompt()` token entry.** Reflects Bible's
    Phase-0 lock that the status page is operator-driven, LAN-only,
    and bearer rotation is the substitute control. FG1 tracks
    replacement with HTML form + same-origin gating post-MVP.
  - **No /stats endpoint yet** — the "Library Stats" panel currently
    derives counts from `/games?limit=1` and `/manifests?limit=1`
    (using the `meta.total` field returned by BL7-BL9 paginated
    endpoints). A dedicated `/stats` endpoint can be added later
    without changing the page contract.
  - **No SSE/WebSocket progress** — polling-only per the spec's
    Post-MVP deferral list.
  - **Page works only with bearer auth.** Loopback-only endpoints
    (`/auth/begin`, etc.) are NOT reachable from the page because
    of the bearer middleware's loopback check on `scope[client]` —
    that's by design for credential-intake paths.

---

<!-- Copy the section above for each new feature. Number sequentially. -->
