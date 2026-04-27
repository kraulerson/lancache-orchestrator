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

<!-- Copy the section above for each new feature. Number sequentially. -->
