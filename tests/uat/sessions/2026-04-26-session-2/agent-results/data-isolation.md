# UAT-2 — Data-Isolation Probe Report

**Agent:** Data-Isolation Probe
**Date:** 2026-04-26
**Scope:** BL3 settings + BL4 db pool — boundary enforcement, leak paths, escalation paths.
**Files audited:**
- `src/orchestrator/core/settings.py`
- `src/orchestrator/db/pool.py`
- `src/orchestrator/db/migrate.py`
- `tests/core/test_settings.py`
- `tests/db/test_pool.py`
- `tests/db/test_pool_property.py`
- `tests/db/test_pool_concurrency.py`
- `tests/db/test_pool_chaos.py`
- `tests/db/test_pool_slow.py`

**Methodology:** trace each entry point, identify the protection, verify the test that proves the protection (or note its absence). Where claimed-protected, run the path through the live code and inspect output.

---

## Boundary 1 — `orchestrator_token` (SecretStr) leak channels

### 1.1 Entry points (places where the raw cleartext could escape Settings)

| Channel | Behavior | Protection | Test |
| --- | --- | --- | --- |
| `repr(s)` | redacted | `pydantic.SecretStr.__repr__` returns `'**********'` | `TestRedaction.test_raw_not_in_repr` (parametrized 5 token shapes) |
| `str(s)` / `f'{s}'` | redacted | inherited from BaseSettings.__str__ via SecretStr | covered indirectly by repr test (same redaction primitive) — **gap: no explicit test** |
| `s.model_dump()` (python mode) | dict contains `SecretStr` object whose `__str__` is masked | SecretStr | `test_raw_not_in_model_dump` |
| `s.model_dump(mode="json")` / `model_dump_json()` | masked `"**********"` | SecretStr.__pydantic_serializer__ | `test_raw_not_in_model_dump_json` |
| `pickle.dumps(s)` | **TypeError** | explicit `__reduce__` override raising | `test_settings_not_pickleable` |
| `copy.deepcopy(s)` / `s.model_copy()` | **clones successfully**; clone yields cleartext via `get_secret_value()` | none | **no test** — see gap below |
| `ValidationError.input_value` (token-related errors) | scrubbed → re-raised as `ValueError` | `Settings.__init__` intercepts errors whose `loc` contains "token" | `test_short_token_validation_error_does_not_echo_raw` |
| Length validator | check runs against `SecretStr` object, not raw str | `_check_token_length` calls `len(v.get_secret_value())` after `_strip_token` | implicit via SEV-2 echo test |
| Log emission | redacted (no field reads `get_secret_value()` for log payload) | structlog binds the `SecretStr` whose `__str__` masks | indirectly covered |

### 1.2 Verified live (Python REPL trace, confirms the design):

- `repr(s)` → `Settings(orchestrator_token=SecretStr('**********'), ...)` ✓
- `s.model_dump_json()` → `"orchestrator_token":"**********"` ✓
- `pickle.dumps(s)` → `TypeError: Settings is not pickle-safe` ✓
- `copy.deepcopy(s).orchestrator_token.get_secret_value()` → cleartext ⚠
- `s.model_copy().orchestrator_token.get_secret_value()` → cleartext ⚠

### 1.3 Gaps

- **G1.1** (low severity): `copy.deepcopy(Settings)` and `Settings.model_copy()` succeed and yield instances whose `get_secret_value()` returns cleartext. This is in-memory cloning so it does not match the on-disk threat model that motivated the `__reduce__` block, but the `__reduce__` docstring frames pickle as the only blocked primitive. Worth either (a) deciding deepcopy is in-scope and overriding `__deepcopy__` similarly, or (b) explicitly documenting "clone in process is fine; cross-process serialization is blocked." Currently neither is asserted by tests.
- **G1.2** (low): `str(s)` / `f"{s}"` paths are not explicitly asserted by tests. Pydantic's `__str__` happens to delegate to `repr` of fields (and SecretStr masks), but a future `__repr__` override that bypasses the model fields would silently regress — caught only by the existing repr test.
- **G1.3** (medium): The token-error scrubber in `Settings.__init__` matches any `loc` containing the substring `"token"` (case-insensitive). This is a heuristic — a future field named e.g. `csrf_token_ttl: int = Field(ge=0)` would have its raw int value scrubbed unnecessarily, and a token leak could escape if a future field is renamed without `"token"` in the name (e.g., `api_credential`). No regression test pins the exact set of fields covered by the scrubber.

### 1.4 Conservative paths — could be simplified

- The `_strip_token` validator in mode="before" branches on `SecretStr | str | other`. The `else` branch is documented as `# pragma: no cover — defensive fallthrough`. It's pragma'd out so coverage doesn't fail; this is fine but adds a small surface for surprise.

---

## Boundary 2 — Reader connections reject writes

### 2.1 Entry points

The pool exposes connections with `PRAGMA query_only = ON` enforced at open time, verified by the readback in `_open_connection`. Any path that smuggles a write through must (a) bypass the PRAGMA or (b) write through a writer connection that was misrouted to a "reader" code path.

| Path | Smuggled write? | Notes |
| --- | --- | --- |
| `pool.read_one / read_all / read_one_as / read_all_as / read_stream` | no | `_checkout_reader` only yields PRAGMA-locked connections |
| `pool.read_transaction()` → `ReadTx` | no | `ReadTx` exposes only `read_*` methods. No `execute`/`execute_many`. Even if a caller tried `tx._conn.execute("INSERT…")`, the underlying connection has `query_only=ON`. **Verified by `test_acquire_reader_query_only_blocks_writes`** for the raw escape hatch. |
| `pool.acquire_reader()` raw escape hatch | **explicitly tested** to reject writes | `test_acquire_reader_query_only_blocks_writes` raises OperationalError("readonly") |
| Schema verification at init (line 641) | uses a reader connection for SELECTs only | safe — `_load_applied_ids_async` is `SELECT id FROM schema_migrations` |
| `pool.schema_status()` | uses `_checkout_reader` for read-only manifest comparison | safe |

### 2.2 Subtle path verified safe

`Pool._open_connection` runs the `query_only` PRAGMA inside the same `for` loop that handles every other PRAGMA — a single failure raises `PoolInitError` and the connection is closed before being returned to the pool. So a half-configured reader cannot leak into the queue. The `_pragma_value_matches` readback verifies `query_only=ON` was actually applied (test: `test_pool_init_error_includes_role_on_pragma_fail`).

### 2.3 Gaps

- **G2.1** (low): No test exercises the case where a caller obtains a `ReadTx` and reaches into `tx._conn` to issue a write. Public-API protection is via "no `execute` method on `ReadTx`," but the underlying connection IS reachable via `_conn` (single-underscore — convention only). The `query_only` PRAGMA still rejects the write, so the property holds, but a "negative test" pinning `tx._conn.execute("UPDATE …")` raises OperationalError would close the inspection gap.
- **G2.2** (low): `ReadTx.read_stream` returns an `AsyncIterator` constructed inside the method body (not via the connection lifecycle). The `try/except` block only catches `aiosqlite.Error` raised when calling `execute()` — exceptions raised mid-iteration (e.g., a disk-I/O error after the first row) won't trigger the connection-replacement state machine because the `except` clause in `_checkout_reader` only sees errors raised inside the `yield`. Streaming generators can raise inside `async for row in cur` — that propagates up; whether it reaches `_checkout_reader`'s except handler depends on timing. Worth a chaos test.

---

## Boundary 3 — `WriteTx` auto-rollback isolation

### 3.1 Mechanism

`Pool.write_transaction` issues `BEGIN IMMEDIATE` after acquiring the writer lock, yields a `WriteTx`, then on exception issues `ROLLBACK`, on success issues `COMMIT`. The writer lock is held for the entire transaction (via `_checkout_writer`'s `async with self._writer_lock`).

### 3.2 Cross-transaction stale-data leak path?

Could a rolled-back transaction return stale data on the next transaction's SELECT?
- The writer connection is reused — same connection across transactions (it's the only one). A `ROLLBACK` on aiosqlite/SQLite drops the in-flight changes and resets the connection state. Subsequent BEGIN IMMEDIATE on the same connection sees the post-rollback state, which is the pre-BEGIN snapshot. **No stale-data leak.**
- `test_write_transaction_rolls_back_on_exception` and `test_write_transaction_rolls_back_on_integrity_error` verify the contract.

### 3.3 Edge case: ROLLBACK fails

The `with contextlib.suppress(Exception):` around `await conn.execute("ROLLBACK")` swallows any error during rollback. If ROLLBACK itself fails (e.g., the connection died mid-transaction), the writer connection is left in an undefined transaction state. The next caller acquiring the writer would issue a fresh `BEGIN IMMEDIATE`, which:
- on a connection with an aborted-but-not-rolled-back transaction, raises "cannot start a transaction within a transaction"
- **`_wrap_aiosqlite_error` would categorize this as `WriteConflictError` (the "database is locked" / "busy" branch doesn't quite match), or fall through to the generic `QueryError`.**

### 3.4 Gaps

- **G3.1** (medium): No test exercises the path where ROLLBACK fails (connection lost mid-transaction). The next write transaction's BEGIN IMMEDIATE could raise an unexpected error, possibly even a misclassified `WriteConflictError`. The chaos suite simulates I/O errors during INSERT/SELECT but not specifically during ROLLBACK. A connection lost during commit/rollback is a known SQLite edge case.
- **G3.2** (low): The `_log.warning("pool.transaction_rolled_back", ...)` event fires inside the except branch, but does NOT include the SQL template or any indicator of what was rolled back. Operationally fine, but means triaging "rollback storms" is hard from logs alone.

---

## Boundary 4 — Concurrent `write_transaction` blocks serialize; no fast-path read of uncommitted state

### 4.1 Serialization mechanism

`_checkout_writer` enters `async with self._writer_lock` before yielding. Two concurrent `write_transaction()` blocks therefore serialize at the asyncio level even before BEGIN IMMEDIATE.

### 4.2 Can a fast-path read see uncommitted writer state?

- Readers go through their own connections — separate from the writer. SQLite WAL semantics guarantee a reader sees the last committed snapshot, never the writer's in-flight changes.
- `test_reads_concurrent_with_writes` (concurrency) verifies: while a writer is mid-transaction (post-INSERT, pre-COMMIT), a reader gets `count=5` (pre-write count), confirming WAL snapshot isolation.
- **Boundary holds.**

### 4.3 Subtle: reads issued INSIDE a write transaction (`WriteTx.read_one`)

The `WriteTx` exposes `read_one`/`read_all`/`read_one_as`/`read_all_as`. These reads run on the writer connection inside the active transaction — so they DO see the in-flight writes. This is documented behavior (`test_write_transaction_supports_reads`) and intentional. From an isolation-boundary view, it's not a leak — the same caller did the write.

### 4.4 Gaps

- **G4.1** (low): No test asserts that `pool.read_one()` (which checks out a separate reader connection) issued from inside a `pool.write_transaction()` block sees the PRE-write snapshot, not the in-flight one. This would be the contract of WAL — verified at the pool level only by the cross-task test (`test_reads_concurrent_with_writes`). The single-task interleaving (writer holds tx, calls `pool.read_one` directly — bypassing `tx`) isn't pinned.

### 4.5 Conservative path — overly defensive

The `_writer_lock` is asyncio-level; SQLite's `BEGIN IMMEDIATE` + `busy_timeout` would already serialize writers at the SQL level. The double-belt-and-braces defense is documented as intentional ("defense-in-depth" per the spec), but means a poorly-behaved consumer that bypasses `write_transaction()` and uses `acquire_writer()` to call `BEGIN IMMEDIATE` directly would still serialize correctly via the SQL layer alone. Leaving as-is is correct — the asyncio lock prevents starvation under high contention because BEGIN IMMEDIATE doesn't queue fairly. **Not a simplification candidate.**

---

## Boundary 5 — `_bg_tasks` use-after-replace

### 5.1 The concern

`_bg_tasks` is a `set[asyncio.Task]` holding fire-and-forget replacement and safe-close coroutines. After `_replace_connection` swaps `self._writer = new_conn` (or replaces a slot in `self._reader_pool`), the OLD connection reference may still be held by:
- the `_safe_close` task currently being executed (closing the old conn)
- anyone holding a reference from before the replacement (e.g., a coroutine that captured `conn` from a `_checkout_*` block but hasn't released yet)

### 5.2 Tracing the lifecycle

In `_checkout_reader`:
```
reader = await self._readers.get()   # captures the old conn ref
try:
    yield reader                       # caller uses it
except (...) as e:
    if is_lost:
        ...spawn_bg(_replace_connection(...))   # replacement starts
    raise
finally:
    if healthy_after:
        await self._readers.put(reader)
```

When a disk-I/O error fires inside the `yield`, the `except` branch sets `healthy_after=False` and spawns the replacement task. The `finally` does NOT put the old reader back — good. **But:** the caller still holds the `reader` reference (passed into `_replace_connection` as `old_conn`), and `_safe_close` runs in the background closing it. If a SECOND coroutine somehow obtained the same `reader` ref before the replacement registered, it would now hold a closed connection.

The pool architecture prevents this: the reader queue is a `Queue` with `maxsize=N`, and `_checkout_reader` removes the connection on `get()` before yielding. While checked out, no other coroutine can `get()` it. After the failure path, the connection is NOT put back — so no other coroutine will see the closed connection via the queue.

The `_reader_pool: list[aiosqlite.Connection]` index list IS mutated by `_replace_connection` (`self._reader_pool[reader_index] = new_conn`). After replacement, `health_check()` iterates `_reader_pool` to send probes — it would probe the NEW connection. **Boundary holds for normal flow.**

### 5.3 Race: `health_check()` started just before replacement

`health_check` snapshots `_reader_pool` via list comprehension (`reader_tasks = [... for i, r in enumerate(self._reader_pool) if self._reader_healthy.get(i, False)]`). If `_replace_connection` runs between the snapshot and the probe, the probe still holds the OLD `r` reference — which `_safe_close` is concurrently closing. The probe's `await conn.execute("SELECT 1")` could race against the close.

This is partially mitigated by `self._reader_healthy.get(i, False)` — `_replace_connection` sets `_reader_healthy[idx] = False` synchronously before spawning the close task. So a probe that started AFTER unhealthy-marking is filtered out. But a probe that already passed the filter (snapshot built first) would proceed against the old conn. The `probe()` function catches `aiosqlite.Error` and returns `(False, str(e))` — so the worst case is a `(False, "connection closed")` reading, not a crash.

### 5.4 Gaps

- **G5.1** (medium): No test specifically exercises "replacement during health_check." `test_health_check_reports_partial_unhealthy_readers` triggers a replacement THEN runs health_check, but doesn't interleave them. A chaos test that fires a replacement mid-`health_check` would close the inspection gap.
- **G5.2** (low): `_safe_close` swallows all exceptions and only logs a warning. If the close hangs and the wait_for(2.0) timeout fires, the connection's underlying socket may leak. Ack'd as best-effort by the spec; no test verifies the timeout fires.

---

## Boundary 6 — `_shape()` and `_template_only()` never leak raw values

### 6.1 `_shape(params)` — verified

Returns type names only. Live trace:
- `_shape({'tok': 'AKIA-LEAK'})` → `{'tok': 'str'}` ✓
- `_shape(['short'])` → `['str']` ✓
- `_shape([1.5e10])` → `['float']` ✓
- `_shape(42)` (single non-iterable, atypical) → `['int']` ✓

Hypothesis property `test_no_raw_value_in_output` covers positional params over a strategy union of `none / bool / int / float / text / binary`. Edge cases:
- Mapping params: `test_named_params_return_type_dict` checks that values are all strings (type names) — but does NOT assert "no raw value appears in the type-name output." A future bug where `_shape({k: v})` returned `{k: f"{type(v).__name__}({v!r})"}` would still pass `isinstance(t, str)`. **Gap G6.1.**
- The "single non-iterable" branch (`_shape(42) → ['int']`) is reachable but not covered by any property-based test — only by the `test_none_params_returns_empty_list` adjacent test. **Gap G6.2.**

### 6.2 `_template_only(sql)` — gaps in literal coverage

Live trace results:

| Input | Output | Concern |
| --- | --- | --- |
| `WHERE x = 'O''Brien'` | `WHERE x = ?` | ✓ correct |
| `WHERE id = 5` | `WHERE id = ?` | ✓ |
| `WHERE x BETWEEN 1 AND 100` | `WHERE x BETWEEN ? AND ?` | ✓ |
| `INSERT VALUES('SECRET','PARAM')` | `INSERT VALUES(?,?)` | ✓ |
| **`WHERE val = 0xDEADBEEF`** | **`WHERE val = 0xDEADBEEF`** | ⚠ **hex literals pass through** |
| **`WHERE x = 1.5e10`** | **`WHERE x = ?.5e1?`** | ⚠ **partial mangling — digits leak** |
| `WHERE x = TRUE` | `WHERE x = TRUE` | low — bool keyword, not a value |
| `INSERT INTO t(name) VALUES('robert'); DROP TABLE t;--')` | `INSERT INTO t(name) VALUES(?); DROP TABLE t;--')` | ⚠ trailing comment-after-quote pass-through |

The Hypothesis property `test_no_string_literals_in_output` only asserts `len(result) <= len(sql)` when there's a `'` in the input — so it doesn't catch the hex/scientific-notation pass-through.
- **Gap G6.3 (medium):** Hex literals (`0x…`) and scientific-notation floats (`1.5e10`) are not normalized. The hex form is uncommon in our codebase (we use parameterized queries everywhere) but an exploratory caller passing a hex blob in raw SQL would have the literal land in logs verbatim.
- **Gap G6.4 (low):** Boolean keywords `TRUE`/`FALSE` (added in SQLite 3.23) pass through. These aren't sensitive but the regex's docstring claims "literal values" — keywords aren't covered.

### 6.3 Validation against the existing parametrized leak test

`test_query_failed_log_does_not_leak_raw_sql_literals` uses `SELECT_IN_LITERAL_NOT_PARAM` (an alphanumeric string in single quotes). That covers the common case. It does not cover hex, scientific-notation, or unquoted numeric literals containing sensitive digits.

### 6.4 Conservative — could be simplified?

The five-alternative regex is reasonable. Adding hex literals (`X'...'` is already covered) and `\b0[xX][0-9a-fA-F]+\b` plus normalizing scientific notation as a single match (`-?\d+(\.\d+)?([eE]-?\d+)?`) would close the gap with one regex tweak. **Recommend: tighten the numeric-literal alternative to consume optional exponent.**

---

## Boundary 7 — Settings singleton thread/await consistency

### 7.1 Mechanism

- `get_settings = lru_cache(Settings)` — `_lru_cache_wrapper` uses an internal RLock; `__call__` and `cache_clear()` are individually thread-safe.
- `reload_settings()` = `cache_clear()` + `get_settings()` — TWO locked operations with a gap.

### 7.2 Threat

During a credential rotation:
1. Operator updates `/run/secrets/orchestrator_token` and calls `reload_settings()`.
2. `cache_clear()` returns. Cache is now empty.
3. Before the subsequent `get_settings()` rebuilds, **thread B** (e.g., a request handler) calls `get_settings()`.
4. Thread B's call hits the empty cache, builds a fresh `Settings()` reading the new file → returns instance X.
5. Thread A's continuation calls `get_settings()` → cache hit on X.

Result: both threads agree on instance X. **No disagreement.** Python's `lru_cache` serialises misses; if A is faster it builds X, B hits X; if B is faster it builds X, A hits X.

### 7.3 BUT: asyncio + threads are different

In `tests/db/test_pool.py::test_init_pool_then_get_pool_returns_singleton`, the test must `monkeypatch` `Settings.model_config` AND call `get_settings.cache_clear()` to force a re-read. This is fragile: if a future test forgets the cache clear, it gets a stale singleton from a prior test's environment. The conftest fixtures DO clear the cache (`tests/core/conftest.py::_isolated_env`, `tests/db/conftest.py::_isolated_env`) — so per-test isolation holds.

### 7.4 await contexts

`get_settings()` is sync; called from both async and sync code. There's no asyncio lock — but `lru_cache` doesn't need one because the function is sync and quick. No async-context divergence is possible.

### 7.5 Gaps

- **G7.1** (low): No test exercises "thread A calls reload while thread B calls get_settings, asserting both observe the new env." This is hard to write deterministically (Python's GIL + lru lock makes the race very narrow), but the threat model says rotation is in scope. A `test_reload_settings_under_concurrent_access` using `concurrent.futures` with two threads would close this.
- **G7.2** (low): `reload_settings()` returns the new instance, but does not `await` any pool-side teardown. If the pool was opened with old settings and `reload_settings` is called, the pool keeps using the old settings (it cached `database_path`, `pool_readers`, etc., at construction time). This is by design — `reload_pool()` is the explicit pool-reload primitive — but no test pins the contract "Settings reload does NOT auto-rebuild the pool." A unit test asserting `reload_settings()` returns a new instance without affecting the existing `_pool` would be defensive.

---

## Cross-boundary: Filesystem-isolation (BL3, migrate.py)

The `_assert_local_filesystem` boundary (refuses NFS/CIFS/etc.) is BL3-scoped and outside the BL4 audit, but worth noting that `verify_schema_current` (called from `Pool._async_create`) does NOT re-check filesystem type — it trusts the caller. If `init_pool()` is called in a context where `run_migrations` was never invoked (e.g., a test that bypasses migration), the pool would happily open over an unsafe filesystem. This is mitigated by the fact that the pool's `Pool.create` requires schema-current state, and schema-current implies migrations ran (which would have failed-closed on NFS in `strict` mode). **Cross-boundary safe.**

---

## Summary of gaps (severity ranked)

| ID | Severity | Boundary | Gap |
| --- | --- | --- | --- |
| G6.3 | medium | `_template_only` | hex literals (`0xDEADBEEF`) pass through; scientific notation only partially normalized (`?.5e1?`) — digits leak |
| G3.1 | medium | WriteTx rollback | no chaos test for ROLLBACK-itself-fails (mid-transaction connection loss) |
| G5.1 | medium | _bg_tasks | no race test for `health_check` interleaved with active replacement |
| G1.3 | medium | token scrubber | substring match on `loc=="token"` is heuristic; future field renames could leak |
| G1.1 | low | SecretStr | `copy.deepcopy` / `model_copy` succeed (in-memory clone — not a disk leak, but undocumented) |
| G2.1 | low | ReadTx writes | no negative test pinning `tx._conn.execute("UPDATE…")` raises |
| G2.2 | low | ReadTx streaming | mid-stream disk-I/O error may not trigger replacement (timing-dependent) |
| G3.2 | low | rollback log | no SQL-template indicator in `pool.transaction_rolled_back` warning |
| G4.1 | low | WAL isolation | no test for "pool.read_one inside write_transaction sees pre-write snapshot" (cross-channel) |
| G5.2 | low | _safe_close | no test verifies `wait_for(2.0)` timeout fires on hung close |
| G6.1 | low | _shape mappings | property test doesn't assert "no raw value in type-name output" for dict params |
| G6.2 | low | _shape single | non-iterable single-param branch (`_shape(42)`) not in property tests |
| G6.4 | low | _template_only | `TRUE`/`FALSE` boolean keywords pass through (not sensitive but undocumented) |
| G7.1 | low | Settings singleton | no concurrent reload+get test |
| G7.2 | low | Settings + Pool | no test pins "reload_settings does not auto-rebuild the pool" |
| G1.2 | low | str(Settings) | no explicit test that `str(s)` / `f"{s}"` redacts |

## Boundaries with no leak path identified
- **B2** Reader query_only: enforced at PRAGMA level + read-back verified + tested against the raw escape hatch.
- **B4 main path** WAL snapshot isolation: verified by `test_reads_concurrent_with_writes`.

## Conservative (worth simplifying?) — none
The dual-belt writer serialization (asyncio lock + BEGIN IMMEDIATE) is justified by the spec; no candidates for relaxation.

## Recommended remediation priority
1. **G6.3** — tighten `_LITERAL_RE` to match hex literals and full scientific notation.
2. **G3.1** — add a chaos test for ROLLBACK failure.
3. **G5.1** — add a chaos test for `health_check` racing with `_replace_connection`.
4. **G1.3** — replace substring `"token"` match with an explicit set of redacted field names; add a regression test that asserts the set.
