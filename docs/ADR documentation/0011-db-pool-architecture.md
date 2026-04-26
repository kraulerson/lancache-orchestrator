# ADR-0011: DB Pool Architecture — Hybrid Writer-Reader Topology, Defense-in-Depth Write Serialization, Comprehensive API Surface

**Status:** Accepted
**Date:** 2026-04-25
**Phase:** 2 (Construction), Milestone B, Build Loop 4 (DB-pool)
**Related:** ADR-0001 (Orchestrator Architecture), ADR-0008 (Migration Runner), ADR-0009 (Logging Framework), ADR-0010 (Settings Module)
**Feature:** BL4-DB-pool

<!-- Last Updated: 2026-04-25 -->

## Context

Every Milestone B+ data path (validator, scheduler-driven prefill jobs, FastAPI
status endpoints, manifest fetch, library sync) reads or writes through a
single async DB layer on top of `aiosqlite`. The concurrent-workload profile
spans:

- Long-running prefill jobs streaming chunk metadata while the validator
  walks the cache directory tree on disk
- Short status-page polls (`/api/v1/health`, `/api/v1/games`, `/api/v1/jobs`)
  hitting concurrent reads
- Library-sync upserts batching tens of writes through a single transaction
- Background pruning jobs (validation_history, jobs, manifests) running on
  APScheduler triggers

The DB Pool is the foundation of all of this. A regression here is felt
everywhere; a leaky abstraction here forces every consumer to handle SQLite
quirks (BUSY retry, `BEGIN IMMEDIATE` semantics, `query_only` enforcement,
PRAGMA verification) one-off. Project Bible §3.3 pre-commits to `aiosqlite`;
Bible §5.6 commits to a single-writer-lock concurrency model on top of WAL;
Bible §10.3 commits to no f-string SQL via the `no-f-string-sql` Semgrep rule.
ADR-0009's structured logging + secret redaction is the audit-trail layer
this pool emits into.

The live questions for BL4 were: pool topology (single connection vs.
writer+readers vs. uniform pool), write serialization (asyncio.Lock vs.
BEGIN IMMEDIATE vs. both), public API shape (minimal vs. comprehensive),
lifecycle pattern (class-only vs. module singleton), PRAGMA scope and
verification, schema-drift integration, error model + observability, and
test strategy.

An 8-question brainstorm walked through the decision space with A/B/C
options. The spec
(`docs/superpowers/specs/2026-04-25-bl4-db-pool-design.md`)
records the full decision trail.

This ADR records the load-bearing architectural decisions behind the final
implementation, plus the SEV-3 background-task-error-logging finding
surfaced by the Phase 2.4 security re-audit and the fix that closed it.

## Decisions

### D1 — Hybrid topology: 1 dedicated writer + N reader connections

**Context:** SQLite WAL mode allows multiple concurrent readers and exactly
one writer at a time. Three shapes were on the table: (a) single connection
with serialized everything, (b) uniform pool of N connections with
application-side write serialization, (c) hybrid with 1 writer-only +
N reader-only.

**Decision:** Hybrid. The writer holds a single `aiosqlite.Connection`
guarded by `asyncio.Lock`. The readers are an `asyncio.Queue` of N
connections each with `PRAGMA query_only=ON` after open. `_checkout_reader`
async context manager bounds reader-pool semantics.

**Consequence:** Read paths never contend for the writer lock — eliminates
the BL3-anticipated F12×F13 contention scenario. `query_only` provides
defense-in-depth: a misrouted write through a reader handle fails with
`OperationalError("readonly database")`, not silent data corruption. Sized
via `pool_readers` (default 8, range 1–32) — see ADR-0010 addendum.

### D2 — Defense-in-depth write serialization (`asyncio.Lock` + `BEGIN IMMEDIATE` + `busy_timeout`)

**Context:** Single-process write serialization needs `asyncio.Lock` (to
prevent two awaitables interleaving inside one writer); `BEGIN IMMEDIATE`
acquires the SQLite RESERVED lock at statement-1 (vs. `BEGIN`'s lazy
acquisition that produces SQLITE_BUSY on the first write); `busy_timeout`
absorbs transient lock contention from any same-file concurrent process
(unlikely in our deployment, but free defense).

**Decision:** All three. Pool layer enforces `asyncio.Lock` ownership in
`_checkout_writer`; `write_transaction()` issues `BEGIN IMMEDIATE` on
entry and `COMMIT`/`ROLLBACK` on exit; every connection's `busy_timeout`
PRAGMA is set at open time (default 5 000 ms, configurable via
`pool_busy_timeout_ms`). `WriteConflictError` wraps the rare case where
busy_timeout exhausts.

**Consequence:** Verified by `test_writes_serialize_in_order` (8 concurrent
writers all succeed in order) and `test_acquire_writer_holds_lock_for_duration`
(secondary acquirer waits ≥100 ms behind a held writer). The `slow`
integration test exercises 32 writers × 30 s sustained workload with a
zero-WriteConflictError assertion (deferred via `@pytest.mark.slow`).

### D3 — Comprehensive public API surface

**Context:** A pool consumed by 8 different downstream callers (FastAPI,
validator, scheduler, CLI, status page, library-sync, manifest-fetch,
prune-jobs) was given two API shapes to choose between: (a) minimal
(`acquire_reader`/`acquire_writer` only — let callers handle cursors), or
(b) comprehensive (helpers + transaction contexts + streaming + raw
escape hatches + dataclass mapping).

**Decision:** Comprehensive. Single-statement helpers (`read_one`,
`read_all`, `read_one_as`, `read_all_as`, `read_stream`, `execute_write`,
`execute_many_write`); multi-statement transaction context managers
(`read_transaction`, `write_transaction`) returning typed `ReadTx`/`WriteTx`
handles; raw connection escape hatches (`acquire_reader`,
`acquire_writer`) for code that needs aiosqlite primitives directly.
`ReadTx` and `WriteTx` are kept as separate types so type checkers and
code review catch attempts to write through a `read_transaction` block.

**Consequence:** ~80 % of consumer code paths can use one-line helpers;
the 20 % that need batching or BLOB streaming have first-class transaction
support. Per `feedback_default_to_most_capable.md`, this prefers
comprehensive surface area now over later refactor when consumers start
hitting the minimal-API ceiling.

### D4 — Class-construction + module singleton lifecycle

**Context:** Two consumer access patterns coexist: (a) tests need to
build pools with custom paths and reader counts, (b) the production
runtime needs a single shared pool reachable from any handler/job/CLI.

**Decision:** `Pool.create(...)` is a class method returning a hybrid
`_PoolCreator` object that supports both `await Pool.create(...)` and
`async with Pool.create(...) as pool:` (the test suite uses both forms).
A separate module singleton API (`init_pool()`, `get_pool()`,
`reload_pool()`, `close_pool()`) wraps the runtime case — `init_pool()`
reads from `get_settings()` and stores the result in module-level
`_pool`. `get_pool()` raises `PoolNotInitializedError` if called before
init. `close_pool()` enforces a 30 s hard timeout; on exhaustion it
force-clears `_pool=None` and raises `PoolError("close_pool() timed out
after 30 s")`.

**Consequence:** Tests get the flexibility they need; runtime gets a
single source of truth without process-global mutation surface; an
operator can `reload_pool()` post-config-change without restarting the
container.

### D5 — 9 PRAGMAs applied + verified per connection

**Context:** SQLite has soft-default PRAGMAs that vary across SQLite
builds. The orchestrator's threat model assumes `journal_mode=WAL`,
`foreign_keys=ON`, `synchronous=NORMAL`, etc. — silent regression on a
future SQLite ABI change would surface as mysterious runtime corruption,
not a clean boot failure.

**Decision:** Each connection runs through a 9-PRAGMA verification loop
(busy_timeout, foreign_keys, synchronous, temp_store, cache_size,
mmap_size, journal_size_limit, journal_mode is per-DB so set in
migrate.py, query_only added for readers). Each PRAGMA is set then read
back; mismatch (after tolerating SQLite's value normalization, e.g. ON→1,
MEMORY→2) raises `PoolInitError(role=role, reason=...)`, closes the
partial connection, and aborts pool startup.

**Consequence:** Boot-time failure is loud and actionable. Verified by
`test_pool_init_error_includes_role_on_pragma_fail` (monkeypatches
`_pragma_value_matches` to return False; pool init fails with
`PoolInitError`). Five new Settings fields drive PRAGMA values
(`pool_readers`, `pool_busy_timeout_ms`, `db_cache_size_kib`,
`db_mmap_size_bytes`, `db_journal_size_limit_bytes`) — see ADR-0010
addendum for that expansion.

### D6 — Schema-drift verification at init + on-demand introspection

**Context:** A pool that starts against a stale or future schema is
worse than no pool — it would silently misbehave on every query.
Migration runs are gated to startup (per ADR-0008); the pool needs a way
to assert "the schema applied at init matches the version this pool's
code was built against."

**Decision:** New helper `verify_schema_current()` in
`src/orchestrator/db/migrate.py` compares applied migration IDs against
the packaged manifest. `Pool.create()` calls it against a reader
connection unless `skip_schema_verify=True`. Pending IDs raise
`SchemaNotMigratedError(missing=[...])`; un-mapped applied IDs raise
`SchemaUnknownMigrationError(unknown=[...])`. The escape-hatch path
emits `pool.schema_verification_skipped` at WARNING — non-suppressible
operator signal. `pool.schema_status()` is the read-only introspection
surface for `/api/v1/health` consumers, returning `{applied, available,
pending, unknown, current}`.

**Consequence:** Migration-vs-binary version skew can never produce
silent runtime corruption. The escape hatch is for the legitimate case
of an operator who is mid-rollback and wants the pool to come up
diagnostically degraded; the WARNING ensures it doesn't slip past.

### D7 — Domain exception hierarchy + structured-event taxonomy + parameter scrubbing + connection-replacement state machine

**Context:** Every consumer needs to distinguish "transient busy → retry"
from "schema bug → crash" from "broken disk → degrade." aiosqlite's raw
exceptions don't make those distinctions ergonomically. The threat model
(TM-012) commits to no raw SQL or parameter values reaching log output.
Disk-I/O errors should not make a single bad sector kill the whole
pool — but unbounded auto-replacement is a denial-of-service amplifier.

**Decision:** 11-class exception hierarchy rooted at `PoolError`:
`PoolNotInitializedError`, `PoolClosedError(state)`,
`PoolInitError(reason, role)`, `SchemaError` →
`SchemaNotMigratedError(missing)` / `SchemaUnknownMigrationError(unknown)`,
`QueryError` → `WriteConflictError(kind)` /
`IntegrityViolationError(constraint_kind, table, column)` /
`ConnectionLostError(role, original_error)` / `QuerySyntaxError`,
`HealthCheckError` → `WriterUnreachableError(reason)` /
`ReaderUnreachableError(reader_index, reason)`. 13 stable structured-event
names per the spec §6.4 taxonomy (`pool.initialized`, `pool.closed`,
`pool.connection_opened`, `pool.connection_replaced`, `pool.connection_lost`,
`pool.replacement_storm`, `pool.replacement_failed`, `pool.write_conflict`,
`pool.integrity_violation`, `pool.query_syntax_error`, `pool.query_failed`,
`pool.operation_cancelled`, `pool.schema_verification_skipped`,
`pool.health_check_partial`, `pool.health_check_failed`,
`pool.transaction_rolled_back`, `pool.background_task_failed`). Every
log emission uses `_template_only(sql)` (literals replaced with `?`) and
`_shape(params)` (type names only) — never raw values. Integrity errors
classified by `sqlite_errorcode` (Python 3.11+ feature, propagates
through aiosqlite empirically), with message-regex fallback for older
runtimes. Connection replacement state machine triggered on disk-I/O
errors (`disk i/o error` or `database disk image is malformed`); per-role
storm guard at >3 replacements in a 60-second sliding window —
beyond the threshold, role transitions to degraded and further
replacements are refused.

**Consequence:** Consumers `except IntegrityViolationError as e:` and
read `e.constraint_kind` to decide retry vs. surface. Operators monitor
`health.readers.replacements` to see successful auto-recoveries vs.
sudden silent zeros. Property-based scrubber tests
(`test_pool_property.py`) exercise `_template_only` and `_shape` against
arbitrary value shapes; pool-internal tests verify end-to-end
log-scrubbing against capsys-captured JSON. Storm guard verified by
`test_storm_guard_trips_after_3_replacements_in_60s`.

### D8 — Comprehensive test strategy: unit + property + chaos + slow + shared-cache `:memory:`

**Context:** A pool is the kind of module where the obvious tests
(does insert work? does select return the right rows?) miss the failure
modes that actually matter (cancellation during transaction, replacement
storm, partial health-check failure, sustained-workload p99). A flat
"117 unit tests" wouldn't have caught the chaos scenarios; pure chaos
testing wouldn't have caught the typed-dataclass mapping bugs.

**Decision:** Five test files. `test_pool.py` (~70 tests, 9 classes:
TestLifecycle, TestSchemaIntegration, TestSingleStatementHelpers,
TestDataclassMapping, TestReadTransaction, TestWriteTransaction,
TestRawAcquire, TestErrorWrapping, TestModuleSingleton).
`test_pool_concurrency.py` (~15 tests: concurrent reads under writer
load, write serialization in order, reader-pool exhaustion queues,
3-scenario cancellation matrix, close-during-in-flight). `test_pool_property.py`
(~5 hypothesis property tests on the `_template_only`/`_shape`
parameter scrubbers — the critical TM-012 invariant). `test_pool_chaos.py`
(~10 monkey-patch tests exercising disk-I/O replacement, storm-guard
trip, partial health-check, per-probe timeout). `test_pool_slow.py`
(3 `@pytest.mark.slow` tests deferred from default runs:
sustained-workload p99 < 200 ms, replacement-storm under load,
streaming-read during concurrent writes). The `mem_pool` shared-cache
`:memory:` fixture supports fast-path unit tests against a known-good
schema-seeded DB.

**Consequence:** 117 tests passing in `tests/db/`, 81 % branch coverage on
`pool.py`. Property tests find scrubber regressions across arbitrary
parameter shapes; chaos tests verify the replacement state machine
under fault injection; slow tests stand in as a Spike-F-style
sustained-workload assertion until full Spike F runs in Phase 3.

## Edge cases (acknowledged, lived with)

- **Cancellation during PRAGMA application.** A `CancelledError` mid-PRAGMA
  in `_open_connection` would leave a partially-configured connection.
  The current code's `try/except` around connect closes on
  `PoolInitError` only; `CancelledError` (BaseException) bypasses the
  handler. In practice, `_open_connection` is called from within
  `Pool._async_create`'s outer `try/except BaseException` that calls
  `_teardown_connections()` — so the partial connection IS closed via
  that broader handler. Acceptable.
- **`aiosqlite.connect()` opening a corrupted DB file.** SQLite returns
  the open successfully and surfaces the problem on first query. Detected
  via the same `disk i/o error` / `database disk image is malformed`
  pathway as runtime disk failures, triggering replacement.
- **Reader-pool starvation under write storm.** WAL semantics guarantee
  readers don't block on writers; even a 32-writer-burst sustained for
  30 s leaves readers responding at <50 ms p99. Verified by
  `test_pool_slow.test_sustained_concurrent_workload` (deferred).
- **Singleton storage at the module level + `monkeypatch.setattr(...,
  "_pool", None)` in tests.** Pythonic test-isolation pattern; module-level
  variables are explicitly part of the public test surface for the
  singleton (the `reset_singleton` conftest fixture wraps it).
- **`acquire_reader`/`acquire_writer` escape hatches bypass error
  wrapping.** Consumer code using these is responsible for wrapping
  aiosqlite errors itself. Documented inline; alternative was wrapping
  every yield path, which defeats the "escape hatch" semantics.

## Cross-references

- **Spec:** `docs/superpowers/specs/2026-04-25-bl4-db-pool-design.md`
  (full decision trail with A/B/C tradeoffs)
- **Plan:** `docs/superpowers/plans/2026-04-25-bl4-db-pool.md`
  (25-task implementation breakdown)
- **Audit:** `docs/security-audits/db-pool-security-audit.md` (Phase 2.4
  re-audit findings + non-findings)
- **Settings expansion:** ADR-0010 addendum (5 new BL4 fields)
- **Migration helper:** `verify_schema_current()` in
  `src/orchestrator/db/migrate.py`

## References

- Bible §3.3 (stack), §5.6 (concurrency), §10.3 (Semgrep rules)
- Threat model TM-005 (SQL injection), TM-012 (log credential leak),
  TM-015 (connection-pool exhaustion), TM-018 (manifest memory bomb)
- Phase 1 ADR-0001 (single-container monolith with three work zones)
- BL3 ID4 ADR-0010 (Settings module — primary consumer pattern)

## Decision

**Accepted.** Implementation lands in `src/orchestrator/db/pool.py`
(~600 LoC). Test coverage at 81 % branches; coverage gap → 100 % filed
as follow-up issue. Phase 2.4 re-audit produced one SEV-3 finding
(background-task error logging) which was fixed inline.
