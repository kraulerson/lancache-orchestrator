# BL4 — Async DB Pool Design Spec

**Date:** 2026-04-25
**Author:** Orchestrator (Karl Raulerson) + AI agent
**Status:** Approved — ready for implementation plan
**Milestone:** B (Construction), Build Loop 4
**Target module:** `src/orchestrator/db/pool.py`
**Brainstorming session:** 2026-04-25 (8 decisions locked)
**Design bias:** Most-capable per `feedback_default_to_most_capable.md` (cross-project preference established 2026-04-25)

---

## 1. Purpose & scope

BL4 builds the async DB pool every Milestone B+ feature reads/writes through. It's foundational — a regression here is felt everywhere, just like BL3's settings module. The pool sits on top of:

- **ID1 migrations** (BL1) — schema applied before pool init
- **ID3 structured logging** (BL2) — pool emits structured events with correlation IDs
- **ID4 settings module** (BL3) — pool reads its config (and is extended with 5 new fields in BL4)

It serves:

- **F9 REST API** — FastAPI handlers' read paths
- **F5/F6 prefill jobs** — multi-statement atomic writes (manifest + game + validation_history + cache_observations)
- **F7 cache validator** — long-running streaming reads
- **F8 block list** — reads + writes
- **F12 scheduler** — job-state transitions
- **F11 CLI** — ad-hoc queries via raw connection access
- **F18 (proposed)** — cache purge, when filed/built (issue #37)

### In scope for BL4

- `Pool` class (~450-550 LoC) with hybrid topology (1 writer + N readers), comprehensive API, full lifecycle
- 5 new Settings fields driving pool tuning
- 11-class exception hierarchy + parameter scrubbing + connection-health replacement
- ~117 net-new tests (107 in new pool test files + 10 extensions to settings/migrate test files)
- ADR-0011, addendum to ADR-0010, CHANGELOG, FEATURES.md Feature 4, README env var rows
- New helper `migrate.verify_schema_current()` — schema drift guard

### Out of scope for BL4

- FastAPI handlers (BL5)
- F18 (cache purge) — depends on BL4 + Spike G; tracked as issue #37
- Spike G (Lancache file-deletion behavior) — runs in parallel; tracked as issue #38
- Schema-altering migrations (none in BL4; the schema set in `0001_initial.sql` covers the pool's needs)

---

## 2. Locked decisions (8 questions)

| # | Area | Decision |
|---|---|---|
| 1 | Pool topology | Hybrid: 1 dedicated writer + N reader connections, `query_only=ON` on readers |
| 2 | Write serialization | Defense-in-depth: `asyncio.Lock` + `BEGIN IMMEDIATE` + `busy_timeout=5000` |
| 3 | Public API shape | Comprehensive: helpers (`read_one/all/stream`, `execute_write/many_write`) + transaction contexts (`read_transaction`, `write_transaction`) + raw `acquire_*` escape hatches + dataclass mapping (`read_one_as`/`read_all_as`) |
| 4 | Lifecycle | Class + module singleton: `Pool.create()` + `init_pool()` / `get_pool()` / `reload_pool()` / `close_pool()` + `health_check()` |
| 5 | PRAGMAs | 9 PRAGMAs (5 from migrate.py + cache_size + mmap_size + journal_size_limit + reader query_only); 5 new Settings fields driving values; PRAGMA values verified post-application |
| 6 | Migration integration | `migrate.verify_schema_current()` enforced at init; `verify_schema=False` escape hatch; `pool.schema_status()` for `/health` |
| 7 | Error model | Domain exception hierarchy (11 classes) + structured logging (13 stable event names) + parameter scrubbing (`_template_only` + `_shape`) + connection-health replacement (with storm-guard at 3-in-60s) + `sqlite_errorcode`-based integrity classification |
| 8 | Tests | ~117 net-new tests + hypothesis property tests + chaos tests + 3 slow integration tests + shared-cache `:memory:` fast-path |

---

## 3. Module shape & public API

### 3.1 File structure

`src/orchestrator/db/pool.py` (single file, ~450-550 LoC). Single-file is preferred over multi-file because the pool's components (Pool class, exception hierarchy, helper functions) are tightly coupled — splitting forces import gymnastics with no real benefit.

### 3.2 Exception hierarchy

```
PoolError(Exception)
├── PoolNotInitializedError
├── PoolClosedError(state: "closing" | "closed")
├── PoolInitError(reason: str, role: "writer" | "reader" | None)
├── SchemaError
│   ├── SchemaNotMigratedError(missing: list[int])
│   └── SchemaUnknownMigrationError(unknown: list[int])
├── QueryError
│   ├── WriteConflictError(kind: "immediate" | "deferred" | "exclusive")
│   ├── IntegrityViolationError(constraint_kind, table, column)
│   ├── ConnectionLostError(role, original_error)
│   └── QuerySyntaxError                    # programmer bug — surfaces to crash
└── HealthCheckError
    ├── WriterUnreachableError(reason)
    └── ReaderUnreachableError(reader_index, reason)
```

All classes preserve underlying aiosqlite cause via `from e` at the wrap site.

### 3.3 Pool class — public method signatures

```python
class Pool:
    @classmethod
    async def create(
        cls,
        *,
        database_path: str | Path,
        readers_count: int,
        busy_timeout_ms: int = 5000,
        cache_size_kib: int = 16384,
        mmap_size_bytes: int = 268_435_456,
        journal_size_limit_bytes: int = 67_108_864,
        skip_schema_verify: bool = False,
    ) -> "Pool": ...

    async def __aenter__(self) -> "Pool": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def close(self) -> None: ...

    # Health & introspection
    async def health_check(self) -> dict: ...
    async def schema_status(self) -> dict: ...

    # Single-statement helpers (auto-wrapped in implicit transactions)
    async def read_one(self, sql: str, params: Sequence | Mapping = ()) -> dict | None: ...
    async def read_all(self, sql: str, params: Sequence | Mapping = ()) -> list[dict]: ...
    async def read_one_as(self, cls: type[T], sql: str, params=()) -> T | None: ...
    async def read_all_as(self, cls: type[T], sql: str, params=()) -> list[T]: ...
    def     read_stream(self, sql: str, params=()) -> AsyncIterator[dict]: ...
    async def execute_write(self, sql: str, params=()) -> int: ...
    async def execute_many_write(self, sql: str, params_seq: Iterable) -> int: ...

    # Multi-statement transactions
    @asynccontextmanager
    async def read_transaction(self) -> AsyncIterator["ReadTx"]: ...

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator["WriteTx"]: ...

    # Raw connection escape hatches
    @asynccontextmanager
    async def acquire_reader(self) -> AsyncIterator[aiosqlite.Connection]: ...

    @asynccontextmanager
    async def acquire_writer(self) -> AsyncIterator[aiosqlite.Connection]: ...
```

### 3.4 Transaction handle types

```python
class ReadTx:
    """Returned from pool.read_transaction(). Read-only operations within a
    consistent snapshot."""
    async def read_one(self, sql, params=()) -> dict | None: ...
    async def read_all(self, sql, params=()) -> list[dict]: ...
    async def read_one_as(self, cls, sql, params=()) -> T | None: ...
    async def read_all_as(self, cls, sql, params=()) -> list[T]: ...
    def     read_stream(self, sql, params=()) -> AsyncIterator[dict]: ...

class WriteTx:
    """Returned from pool.write_transaction(). Auto-commit on context exit;
    auto-rollback on exception."""
    async def execute(self, sql, params=()) -> int: ...
    async def execute_many(self, sql, params_seq) -> int: ...
    # Reads within a write transaction hit the writer connection
    async def read_one(self, sql, params=()) -> dict | None: ...
    async def read_all(self, sql, params=()) -> list[dict]: ...
    async def read_one_as(self, cls, sql, params=()) -> T | None: ...
    async def read_all_as(self, cls, sql, params=()) -> list[T]: ...
```

`ReadTx` and `WriteTx` are kept as separate types (not unified `Tx`) so type-checkers and code review catch attempts to write through a `read_transaction`.

### 3.5 Module-level singleton API

```python
async def init_pool(*, verify_schema: bool = True) -> Pool: ...
def     get_pool() -> Pool: ...                      # raises PoolNotInitializedError
async def reload_pool() -> Pool: ...
async def close_pool() -> None: ...                  # 30s hard timeout
```

Singleton storage: module-level `_pool: Pool | None = None`, protected by `asyncio.Lock` so concurrent first-call races resolve to a single instance.

### 3.6 Imports allowed

- Standard library: `asyncio`, `contextlib` (asynccontextmanager), `dataclasses`, `pathlib`, `re`, `time`, `typing` (AsyncIterator, TypeVar, Any, Sequence, Mapping)
- Third-party: `aiosqlite`, `structlog`
- First-party: `orchestrator.core.settings.get_settings`, `orchestrator.db.migrate.verify_schema_current` (new helper)

---

## 4. Internal architecture

### 4.1 Pool instance state

```python
class Pool:
    # Configuration (immutable after create)
    _database_path: Path
    _readers_count: int
    _busy_timeout_ms: int
    _cache_size_kib: int
    _mmap_size_bytes: int
    _journal_size_limit_bytes: int

    # Connections
    _writer: aiosqlite.Connection
    _writer_lock: asyncio.Lock                    # Q2 in-process serialization
    _readers: asyncio.Queue[aiosqlite.Connection]

    # Lifecycle state
    _state: Literal["initializing", "ready", "closing", "closed"]
    _state_lock: asyncio.Lock
    _close_event: asyncio.Event

    # Health & replacement
    _writer_healthy: bool
    _reader_healthy: dict[int, bool]
    _replacement_count: dict[Literal["writer", "reader"], int]
    _replacement_timestamps: dict[Literal["writer", "reader"], list[float]]   # for storm guard

    # Metrics
    _created_monotonic: float
    _total_writes: int
    _total_reads: int
```

### 4.2 Reader pool: `asyncio.Queue` semantics

Readers checked out via `await self._readers.get()`, returned via `await self._readers.put(reader)`. Queue capacity == reader count; auto-bounded. All reader-using helpers route through `_checkout_reader()` async context manager — single choke point for lock semantics, replacement, and exception wrapping.

```python
@asynccontextmanager
async def _checkout_reader(self) -> AsyncIterator[aiosqlite.Connection]:
    if self._state != "ready":
        raise PoolClosedError(state=self._state)
    reader = await self._readers.get()
    healthy_after = True
    try:
        yield reader
    except aiosqlite.OperationalError as e:
        if "disk i/o error" in str(e).lower() or "database disk image is malformed" in str(e).lower():
            healthy_after = False
            await self._replace_connection(role="reader", reader_index=self._index_of(reader),
                                            old_conn=reader)
        raise
    finally:
        if healthy_after:
            await self._readers.put(reader)
```

### 4.3 Writer: single connection under `_writer_lock`

```python
@asynccontextmanager
async def _checkout_writer(self) -> AsyncIterator[aiosqlite.Connection]:
    if self._state != "ready":
        raise PoolClosedError(state=self._state)
    async with self._writer_lock:
        try:
            yield self._writer
        except aiosqlite.OperationalError as e:
            if "disk i/o error" in str(e).lower() or "database disk image is malformed" in str(e).lower():
                await self._replace_connection(role="writer", old_conn=self._writer)
            raise
```

`asyncio.CancelledError` during the `async with` releases the lock cleanly.

### 4.4 Write transaction structure

```python
@asynccontextmanager
async def write_transaction(self) -> AsyncIterator["WriteTx"]:
    async with self._checkout_writer() as conn:
        await conn.execute("BEGIN IMMEDIATE")        # Q2 layer 2 (engine-level)
        tx = WriteTx(conn=conn, pool=self)
        try:
            yield tx
        except BaseException:
            await conn.execute("ROLLBACK")
            log.warning("pool.transaction_rolled_back",
                        role="writer", correlation_id=_get_cid())
            raise
        else:
            await conn.execute("COMMIT")
```

`BaseException` (not `Exception`) ensures `KeyboardInterrupt` and `asyncio.CancelledError` also trigger rollback.

### 4.5 Connection-open sequence (PRAGMA application)

```python
async def _open_connection(self, role: Literal["writer", "reader"]) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(str(self._database_path))
    conn.row_factory = aiosqlite.Row
    pragmas = [
        ("busy_timeout", self._busy_timeout_ms),
        ("foreign_keys", "ON"),
        ("synchronous", "NORMAL"),
        ("temp_store", "MEMORY"),
        ("cache_size", -self._cache_size_kib),
        ("mmap_size", self._mmap_size_bytes),
        ("journal_size_limit", self._journal_size_limit_bytes),
    ]
    if role == "reader":
        pragmas.append(("query_only", "ON"))

    applied = {}
    for name, expected in pragmas:
        await conn.execute(f"PRAGMA {name} = {expected}")
        # Verify by reading back
        async with conn.execute(f"PRAGMA {name}") as cur:
            row = await cur.fetchone()
            actual = row[0] if row else None
            applied[name] = actual
            # PRAGMA values can normalize (e.g., "ON" → 1, "MEMORY" → 2);
            # the verify check tolerates known normalizations via a helper.
            if not _pragma_value_matches(name, expected, actual):
                log.critical("pool.pragma_mismatch", role=role, pragma=name,
                             expected=expected, actual=actual)
                await conn.close()
                raise PoolInitError(
                    reason=f"PRAGMA {name} verify failed: expected {expected!r}, got {actual!r}",
                    role=role,
                )

    log.info("pool.connection_opened",
             role=role,
             pragmas_applied=applied)
    return conn
```

`_pragma_value_matches` is a small helper that handles SQLite's type-normalization (e.g., `"ON"` reads back as `1`, `"MEMORY"` reads back as `2` for `temp_store`). The mismatch path raises `PoolInitError(role=role, reason="pragma_application_failed: ...")`, closes the partial connection, and aborts pool startup. This is the defense against silent SQLite version differences (a future SQLite ABI change that drops support for one of these PRAGMAs would surface at boot, not as a mysterious runtime issue).

`journal_mode = WAL` is **not** applied per-connection — it's a database-level property already set by `run_migrations()` and persists in the DB file.

### 4.6 Connection replacement state machine

Triggered exclusively from the `_checkout_*` exception handlers when an OperationalError matches "disk i/o error" or "database disk image is malformed".

```python
async def _replace_connection(
    self,
    *,
    role: Literal["writer", "reader"],
    reader_index: int | None = None,
    old_conn: aiosqlite.Connection,
) -> None:
    # Storm guard
    now = time.monotonic()
    self._replacement_timestamps[role].append(now)
    recent = [t for t in self._replacement_timestamps[role] if now - t < 60]
    self._replacement_timestamps[role] = recent
    if len(recent) > 3:
        log.critical("pool.replacement_storm",
                     role=role,
                     count_in_60s=len(recent))
        if role == "writer":
            self._writer_healthy = False
        else:
            self._reader_healthy[reader_index] = False
        # Pool transitions to degraded; future ops on this role raise
        return

    # Mark old as unhealthy (synchronous; no awaits before swap)
    if role == "writer":
        self._writer_healthy = False
    else:
        self._reader_healthy[reader_index] = False

    # Background close (best-effort)
    asyncio.create_task(self._safe_close(old_conn, role=role))

    # Open replacement
    try:
        new_conn = await self._open_connection(role=role)
    except Exception as e:
        log.critical("pool.replacement_failed",
                     role=role, reader_index=reader_index, reason=str(e))
        return

    # Atomic swap
    if role == "writer":
        self._writer = new_conn
        self._writer_healthy = True
    else:
        await self._readers.put(new_conn)
        self._reader_healthy[reader_index] = True

    self._replacement_count[role] += 1
    log.warning("pool.connection_replaced",
                role=role, reader_index=reader_index,
                replacement_count=self._replacement_count[role])
```

**Storm-guard threshold: 3 replacements within 60 seconds.** Triggers degraded state; operator must `reload_pool()` to recover.

### 4.7 Cancellation matrix

| Scenario | Handling |
|---|---|
| `CancelledError` during single-statement read | Reader returned to queue (still healthy); re-raise. Log `pool.operation_cancelled` (role=reader, transaction_active=False). |
| `CancelledError` during streaming read | Generator closed; reader returned; cursor closed in finally; re-raise. |
| `CancelledError` during single-statement write | Best-effort `ROLLBACK` (10ms timeout); writer_lock released by `async with`; re-raise. Log `transaction_active=True` if BEGIN had been sent. |
| `CancelledError` during `write_transaction` block | Same; `WriteTx.__aexit__` runs the rollback path. |
| `CancelledError` during `health_check` | All probes cancelled via gather propagation; log `pool.health_check_cancelled`. |
| `CancelledError` during `init_pool` | Close already-opened connections in parallel; raise `PoolInitError(reason="cancelled")`. |
| `CancelledError` during `close_pool` | Uninterruptible — finish closing, log `pool.close_uninterruptible`. **30s hard timeout** ceiling: if exceeded, force-clear `_pool=None`, raise `PoolError("close_pool() timed out after 30s")`. |

### 4.8 Health check implementation

```python
async def health_check(self) -> dict:
    async def probe_one(conn: aiosqlite.Connection, role: str, idx: int | None) -> tuple[bool, str | None]:
        try:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=1.0)
            return (True, None)
        except (aiosqlite.Error, asyncio.TimeoutError) as e:
            return (False, str(e))

    # Probe writer + each reader concurrently
    writer_task = asyncio.create_task(probe_one(self._writer, "writer", None))
    # Drain readers temporarily (each probe checks one reader)
    # ... (implementation detail: snapshot reader connections, probe in parallel)
    results = await asyncio.gather(writer_task, *reader_tasks, return_exceptions=True)

    return {
        "writer": {
            "healthy": results[0][0],
            "replacements": self._replacement_count["writer"],
        },
        "readers": {
            "total": self._readers_count,
            "healthy": sum(1 for _, healthy_tuple in reader_results if healthy_tuple[0]),
            "replacements": self._replacement_count["reader"],
        },
        "schema": await self.schema_status(),
        "uptime_sec": int(time.monotonic() - self._created_monotonic),
    }
```

Per-probe 1-second timeout prevents hung connections from deadlocking the endpoint. Concurrent execution prevents one slow probe from blocking others. A failed probe marks the connection unhealthy + triggers async replacement; `health_check` returns the *current* state, not post-recovery.

---

## 5. PRAGMA application + Settings expansion + migration integration

### 5.1 Settings — 5 new fields

Adds to `src/orchestrator/core/settings.py`:

| Field | Type | Default | Bounds | Env var |
|---|---|---|---|---|
| `pool_readers` | `int` | `8` | 1..32 | `ORCH_POOL_READERS` |
| `pool_busy_timeout_ms` | `int` | `5000` | 0..60000 | `ORCH_POOL_BUSY_TIMEOUT_MS` |
| `db_cache_size_kib` | `int` | `16384` | 1024..1048576 | `ORCH_DB_CACHE_SIZE_KIB` |
| `db_mmap_size_bytes` | `int` | `268_435_456` | 0..17_179_869_184 | `ORCH_DB_MMAP_SIZE_BYTES` |
| `db_journal_size_limit_bytes` | `int` | `67_108_864` | 1_048_576..1_073_741_824 | `ORCH_DB_JOURNAL_SIZE_LIMIT_BYTES` |

**New diagnostic warning** (joining BL3's existing 4):

```python
if self.pool_readers > self.chunk_concurrency:
    log.warning(
        "config.pool_readers_over_provisioned",
        pool_readers=self.pool_readers,
        chunk_concurrency=self.chunk_concurrency,
        hint="pool_readers > chunk_concurrency means readers will idle"
    )
```

**Memory baseline** (documented in FEATURES.md Feature 3 + README): `(pool_readers + 1) × db_cache_size_kib + db_mmap_size_bytes`. Default config = `9 × 16 MiB + 256 MiB ≈ 400 MiB` resident.

### 5.2 Cross-cutting Settings expansion impact

| File | Change |
|---|---|
| `src/orchestrator/core/settings.py` | +5 fields with `Field(...)` constraints, +1 warning emission |
| `tests/core/test_settings.py` | +5 default-test parametrize entries, +5 boundary tests, +1 warning test |
| `FEATURES.md` Feature 3 | +5 env var rows + memory-baseline note |
| `README.md` | +5 env var quick-reference rows |
| `docs/ADR documentation/0010-settings-module-design.md` | Append "post-merge addendum" (~30 lines) noting BL4 expansion |

### 5.3 Migration integration

**New function in `src/orchestrator/db/migrate.py`:** `verify_schema_current()`

```python
async def verify_schema_current(conn: aiosqlite.Connection) -> None:
    """Assert the database schema matches the packaged migration manifest.

    Raises:
      - SchemaNotMigratedError(missing=[...]) if applied is a strict subset
      - SchemaUnknownMigrationError(unknown=[...]) if applied has IDs not in
        the available manifest
    """
    applied_ids = await _load_applied_ids_async(conn)
    available_ids = _load_available_ids()
    missing = available_ids - applied_ids
    unknown = applied_ids - available_ids
    if missing:
        from orchestrator.db.pool import SchemaNotMigratedError
        raise SchemaNotMigratedError(missing=sorted(missing))
    if unknown:
        from orchestrator.db.pool import SchemaUnknownMigrationError
        raise SchemaUnknownMigrationError(unknown=sorted(unknown))
```

Reuses existing `_load_checksum_manifest()` + manifest helpers in migrate.py.

**Pool init flow:**

```python
async def init_pool(*, verify_schema: bool = True) -> Pool:
    settings = get_settings()
    pool = await Pool.create(
        database_path=settings.database_path,
        readers_count=settings.pool_readers,
        busy_timeout_ms=settings.pool_busy_timeout_ms,
        cache_size_kib=settings.db_cache_size_kib,
        mmap_size_bytes=settings.db_mmap_size_bytes,
        journal_size_limit_bytes=settings.db_journal_size_limit_bytes,
        skip_schema_verify=not verify_schema,
    )
    if verify_schema:
        async with pool.acquire_reader() as conn:
            await migrate.verify_schema_current(conn)
    else:
        log.warning("pool.schema_verification_skipped", caller="init_pool")
    return pool
```

**`pool.schema_status()`** — read-only introspection for `/health` consumers; returns `{"applied": [...], "available": [...], "pending": [...], "unknown": [...], "current": bool}`.

### 5.4 Boot order (production)

```python
async def main() -> int:
    configure_logging(log_level=get_settings().log_level)
    log = structlog.get_logger()

    log.info("boot.migrations_starting")
    try:
        await asyncio.to_thread(run_migrations, get_settings().database_path)
    except MigrationError as e:
        log.critical("boot.migrations_failed", reason=str(e))
        return 1

    log.info("boot.pool_starting")
    try:
        pool = await init_pool()
    except (SchemaNotMigratedError, SchemaUnknownMigrationError, PoolError) as e:
        log.critical("boot.pool_init_failed", reason=str(e))
        return 1

    try:
        await serve_forever(pool)
    finally:
        await close_pool()
    return 0
```

Explicit, auditable boot order. `run_migrations` (sync) wrapped in `asyncio.to_thread` to avoid blocking the loop.

---

## 6. Error model + observability

### 6.1 Error wrapping dispatch

Single function `_wrap_aiosqlite_error()` (private module function) is the only place aiosqlite errors get categorized. Used by every code path that touches aiosqlite. Categorization changes propagate everywhere automatically.

### 6.2 Integrity error classification (Q7 + spike result)

Uses `sqlite_errorcode` (Python 3.11+ feature, propagates through aiosqlite — empirically verified):

```python
_CONSTRAINT_KIND_BY_CODE = {
    275:  "check",       # SQLITE_CONSTRAINT_CHECK
    787:  "fk",          # SQLITE_CONSTRAINT_FOREIGNKEY
    1299: "notnull",     # SQLITE_CONSTRAINT_NOTNULL
    1555: "primarykey",  # SQLITE_CONSTRAINT_PRIMARYKEY
    2067: "unique",      # SQLITE_CONSTRAINT_UNIQUE
}

def _classify_integrity_error(e: aiosqlite.IntegrityError) -> tuple[str, str | None, str | None]:
    code = getattr(e, 'sqlite_errorcode', None)
    kind = _CONSTRAINT_KIND_BY_CODE.get(code, "unknown") if code is not None else "unknown"
    m = re.search(r"constraint failed:\s+(\w+)\.(\w+)", str(e))
    table, column = (m.group(1), m.group(2)) if m else (None, None)
    return kind, table, column
```

`sqlite_errorcode`-based classification chosen over message-regex because:
- More accurate — PRIMARY KEY violations carry message "UNIQUE constraint failed: t.id" but code 1555 (PRIMARYKEY)
- Version-stable — extended result codes are SQLite ABI; message text could change
- Falls through cleanly when code is missing or unrecognized → `kind="unknown"`

Table/column extraction still uses regex because `sqlite_errorcode` doesn't carry that info.

### 6.3 Parameter scrubbing helpers

```python
_LITERAL_RE = re.compile(
    r"""
    '(?:''|[^'])*'           # 'string literals' with '' escape
    | "(?:""|[^"])*"         # "identifiers"
    | \b\d+(?:\.\d+)?\b      # numeric literals
    | \bX'[0-9a-fA-F]+'      # X'hex' BLOB literals
    | \bNULL\b               # NULL keyword in literal context
    """,
    re.VERBOSE | re.IGNORECASE,
)

def _template_only(sql: str) -> str:
    """Replace literal values in SQL with '?' placeholders."""
    return _LITERAL_RE.sub("?", sql)

def _shape(params) -> list[str] | dict[str, str]:
    """Return parameter type names only — never values."""
    if params is None:
        return []
    if isinstance(params, Mapping):
        return {k: type(v).__name__ for k, v in params.items()}
    return [type(p).__name__ for p in params]
```

**Critical invariant** (regression-tested): every aiosqlite error wrap MUST log only `_template_only(sql)` and `_shape(params)`, never raw SQL or raw parameter values.

### 6.4 Structured event taxonomy (13 stable names)

| Event | Level | Trigger |
|---|---|---|
| `pool.initialized` | INFO | After `Pool.create()` succeeds |
| `pool.closed` | INFO | After `close_pool()` completes |
| `pool.close_timed_out` | ERROR | `close_pool()` exceeded 30s ceiling |
| `pool.connection_opened` | INFO | Each successful connection open + PRAGMA verify |
| `pool.connection_replaced` | WARNING | Auto-replacement triggered |
| `pool.connection_lost` | ERROR | Per-query connection death |
| `pool.replacement_storm` | CRITICAL | Storm guard tripped (>3 in 60s) |
| `pool.replacement_failed` | CRITICAL | Replacement open() failed |
| `pool.write_conflict` | ERROR | busy_timeout exhausted on writer |
| `pool.integrity_violation` | ERROR | aiosqlite IntegrityError wrapped |
| `pool.query_syntax_error` | CRITICAL | aiosqlite OpError indicating bug |
| `pool.query_failed` | ERROR | Catch-all for unrecognized aiosqlite errors |
| `pool.operation_cancelled` | WARNING | asyncio.CancelledError during op |
| `pool.schema_verification_skipped` | WARNING | `verify_schema=False` invoked |
| `pool.health_check_partial` | WARNING | Some readers unhealthy, writer OK |
| `pool.health_check_failed` | ERROR | Writer down or no healthy readers |

ADR-0011 documents these verbatim. Adding new events is non-breaking; renaming/removing IS — operators may build alerts on specific names.

---

## 7. Test strategy

### 7.1 Test files

| File | Tests | Purpose |
|---|---|---|
| `tests/db/test_pool.py` | ~70 | Lifecycle, API helpers, transactions, error wrapping, schema integration, health check |
| `tests/db/test_pool_concurrency.py` | ~15 | Concurrent reads, write serialization, reader exhaustion, replacement under load, cancellation matrix |
| `tests/db/test_pool_property.py` | ~5 | Hypothesis property tests for `_template_only`, `_shape`, `_classify_integrity_error` |
| `tests/db/test_pool_chaos.py` | ~10 | Connection-replacement state machine, storm guard, partial health-check failures |
| `tests/db/test_pool_slow.py` | ~3 | `@pytest.mark.slow` Spike-F-style sustained workload |
| `tests/core/test_settings.py` | +6 | 5 new field tests + 1 over-provisioning warning test |
| `tests/db/test_migrate.py` | +4 | `verify_schema_current()` happy path + 3 failure modes |

**Total net-new BL4 tests: ~117** (107 in new pool test files + 10 extensions to existing files).

### 7.2 Fixture infrastructure

`tests/db/conftest.py` extended (existing autouse `_isolated_env` from #23 retained):

- `db_path` — fresh DB file per test, migrations applied
- `pool` — standard tmp-file pool, ~50ms setup, realistic semantics
- `mem_pool` — shared-cache `:memory:` pool, ~5ms setup, for pure-API tests
- `populated_pool` — tmp-file pool seeded with realistic test data (5 games, 3 manifests, 2 jobs, 4 cache_observations)
- `reset_singleton` — resets module-level `_pool` between tests exercising the singleton

### 7.3 Slow integration tests (`@pytest.mark.slow`)

Three tests, run via `pytest --run-slow`, separate CI job:

1. **`test_sustained_concurrent_workload`** — 32 concurrent writers × 4-statement transactions × 30 seconds. Asserts: zero `WriteConflictError`, p99 transaction <200ms (CI margin; design 100ms), reader p99 <50ms during writer load.
2. **`test_replacement_storm_guard_under_load`** — pathologically broken connection forces 4+ replacements in 60s; storm guard trips; pool transitions to degraded.
3. **`test_long_running_streaming_read_under_concurrent_writes`** — 30s `read_stream` while writes hit writer; verifies streaming doesn't block on writer activity (WAL semantics).

Pytest marker config:

```toml
[tool.pytest.ini_options]
markers = [
    "slow: marks tests that simulate sustained workload (deselect with -m 'not slow')",
]
```

### 7.4 New dev dependency

`hypothesis==6.152.2` added to `requirements-dev.in`. Lockfile regenerated with `pip-compile --allow-unsafe --generate-hashes`. Verified available 2026-04-25.

### 7.5 Coverage target

100% branch coverage on `pool.py` + 100% line coverage on the error-wrapping helpers (`_classify_integrity_error`, `_template_only`, `_shape`, `_wrap_aiosqlite_error`). Helpers are pure and small; partial coverage there is a smell.

---

## 8. Documentation deliverables

| Artifact | Lines | Status |
|---|---|---|
| This spec | ~700 | Written this session |
| `docs/superpowers/plans/2026-04-25-bl4-db-pool.md` | ~1500-2000 | To be written by writing-plans skill |
| `docs/ADR documentation/0011-db-pool-architecture.md` | ~150 | Written during BL4 docs phase |
| `docs/ADR documentation/0010-settings-module-design.md` | +30 | Append "post-merge addendum" |
| `docs/security-audits/bl4-db-pool-security-audit.md` | ~80 | Post Phase 2.4 audit |
| `CHANGELOG.md` | +50 | Security/Added/Infrastructure/Documentation |
| `FEATURES.md` Feature 4 | ~80 | New section, mirrors Feature 3 structure |
| `FEATURES.md` Feature 3 | +10 | 5 new env var rows + memory baseline |
| `README.md` | +10 | 5 new env var rows |
| `PROJECT_BIBLE.md` §5 | +5 | Cross-reference F18 + Spike G as MVP additions |

### 8.1 ADR-0011 outline

```
# ADR-0011: DB Pool Architecture

## Context
- BL4 builds the async DB pool consumed by every Milestone B+ feature.
- 8-question brainstorm, all decisions chose the comprehensive option per
  feedback_default_to_most_capable.md.

## Decisions
D1 — Hybrid pool topology: 1 dedicated writer + N reader connections,
     query_only=ON on readers
D2 — Defense-in-depth write serialization: asyncio.Lock + BEGIN IMMEDIATE +
     busy_timeout=5000
D3 — Comprehensive public API: helpers + transaction context + streaming +
     raw acquire + dataclass mapping
D4 — Full lifecycle: Pool class + module singleton + class-as-context-manager +
     health_check
D5 — 9-PRAGMA set + 5 new Settings fields driving values
D6 — Strict schema verification at init via migrate.verify_schema_current() +
     verify_schema=False escape hatch
D7 — Domain exception hierarchy + structured logging + parameter scrubbing +
     connection-health replacement

## Edge cases
E1 — Replacement-storm guard at 3-in-60s
E2 — Integrity error classification via sqlite_errorcode (Python 3.11+
     feature, empirically verified during brainstorm spike)
E3 — close_pool() hard 30s timeout
E4 — Cancellation matrix (12 scenarios)

## Cross-references
- ADR-0001 (architecture): pool consumes asyncio loop discipline
- ADR-0010 (settings): extended by 5 new fields; addendum filed
- ID1 migrate.py: pool depends on verify_schema_current() (new helper)

## References
- Spec: docs/superpowers/specs/2026-04-25-bl4-db-pool-design.md
- Plan: docs/superpowers/plans/2026-04-25-bl4-db-pool.md
- Audit: docs/security-audits/bl4-db-pool-security-audit.md
```

---

## 9. Follow-up issues

Anticipated SEV-3/SEV-4 items to file at BL4 close:

1. **SEV-4** — `_template_only` regex coverage (expand on first observed regression for exotic SQL forms).
2. **SEV-4** — Prometheus exporter consuming the structured event taxonomy. Operator-tooling enhancement.
3. **SEV-3** — `pool.health_check()` should expose disk-space metrics for the DB volume. Real ops signal (DB volume nearly full → write conflicts spike).
4. **SEV-4** — Per-query timing histograms in structured events. `pool.query_completed` event with `duration_ms`. Useful for slow-query analysis.

---

## 10. Memory artifact

At BL4 close, save `project_bl4_db_pool_complete.md` mirroring BL3 pattern:
- 8 locked decisions (one-line each)
- Build Loop commit hashes
- Spike G outcome (if it ran in parallel, otherwise marked "deferred")
- Follow-up issue numbers
- Non-obvious learnings discovered during execution

Also save to Qdrant for cross-project searchability (per cross-store policy established 2026-04-25).

---

## 11. Commit plan

Anticipated 8-commit sequence (each gets A/B/C structure approval at fire time):

1. `docs(spec): BL4 DB pool design — 8 decisions, hybrid topology` — this spec
2. `docs(plan): BL4 DB pool — N-task implementation plan` — writing-plans output
3. `feat(core): extend Settings with 5 BL4 pool fields` — precursor with ADR-0010 addendum, FEATURES/README updates
4. `test(db): BL4 DB pool failing test suite` — TDD red phase
5. `feat(db): BL4 DB pool implementation — pool.py + verify_schema_current() helper`
6. `fix(db): BL4 security re-audit findings` — if any
7. `docs(adr,changelog,features): BL4 — ADR-0011 + Feature 4`
8. `chore(framework): close BL4 process checklist + record DB-pool`

---

## 12. Definition of done

- [ ] All ~117 tests pass (default `pytest` excludes slow)
- [ ] 100% branch coverage on `pool.py` + 100% line coverage on error-wrapping helpers
- [ ] Slow integration tests pass locally (`pytest --run-slow`)
- [ ] Security re-audit returns no SEV-1/SEV-2 findings
- [ ] ADR-0011 + ADR-0010 addendum committed
- [ ] CHANGELOG `[Unreleased]` extended
- [ ] FEATURES.md Feature 4 added + Feature 3 extended with 5 new env var rows
- [ ] README.md env var table extended
- [ ] PROJECT_BIBLE.md §5 cross-references F18 + Spike G
- [ ] 4 follow-up issues filed
- [ ] `scripts/test-gate.sh --record-feature "DB-pool"` runs (will trigger UAT-2; counter advances to 2)
- [ ] All 6 Phase 2 checklist steps fired
- [ ] Memory `project_bl4_db_pool_complete.md` written + Qdrant store
- [ ] Branch pushed + PR opened (Orchestrator merges per `feedback_pr_merge_ownership`)
- [ ] **UAT-2 testing session begins** after BL4 ships — next Phase 2 framework step
