"""orchestrator.db.pool — async DB pool (BL4).

Hybrid topology: 1 dedicated writer connection + N reader connections.
Defense-in-depth write serialization: asyncio.Lock + BEGIN IMMEDIATE +
busy_timeout=5000. Comprehensive API: helpers + transaction context +
streaming + raw acquire + dataclass mapping. 11-class exception hierarchy
with sqlite_errorcode-based integrity classification.

See docs/superpowers/specs/2026-04-25-bl4-db-pool-design.md for full design.
See docs/ADR documentation/0011-db-pool-architecture.md for the ADR.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncIterator, Generator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import fields
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import aiosqlite
import structlog

from orchestrator.core.settings import get_settings

if TYPE_CHECKING:
    from pathlib import Path

T = TypeVar("T")

_log = structlog.get_logger(__name__)


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class PoolError(Exception):
    """Base for all pool errors."""


class PoolNotInitializedError(PoolError):
    """init_pool() has not been called yet."""


class PoolClosedError(PoolError):
    def __init__(self, state: str) -> None:
        super().__init__(f"pool is {state}")
        self.state = state


class PoolInitError(PoolError):
    def __init__(self, reason: str, role: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.role = role


class SchemaError(PoolError):
    """Base for schema verification errors."""


class SchemaNotMigratedError(SchemaError):
    def __init__(self, missing: list[int]) -> None:
        super().__init__(f"pending migrations: {missing}")
        self.missing = missing


class SchemaUnknownMigrationError(SchemaError):
    def __init__(self, unknown: list[int]) -> None:
        super().__init__(f"applied migrations not in manifest: {unknown}")
        self.unknown = unknown


class QueryError(PoolError):
    """Base for query-time errors."""


class WriteConflictError(QueryError):
    def __init__(self, kind: str = "immediate") -> None:
        super().__init__(f"write conflict (BEGIN {kind.upper()} timed out)")
        self.kind = kind


class IntegrityViolationError(QueryError):
    def __init__(
        self,
        constraint_kind: str,
        table: str | None = None,
        column: str | None = None,
    ) -> None:
        msg = f"{constraint_kind} constraint failed"
        if table:
            msg += f" on {table}"
            if column:
                msg += f".{column}"
        super().__init__(msg)
        self.constraint_kind = constraint_kind
        self.table = table
        self.column = column


class ConnectionLostError(QueryError):
    def __init__(self, role: str, original_error: str = "") -> None:
        super().__init__(f"{role} connection lost: {original_error}")
        self.role = role
        self.original_error = original_error


class QuerySyntaxError(QueryError):
    """Programmer-bug-class error: bad SQL, missing table, missing column."""


class HealthCheckError(PoolError):
    """Base for health-check probe failures."""


class WriterUnreachableError(HealthCheckError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"writer probe failed: {reason}")
        self.reason = reason


class ReaderUnreachableError(HealthCheckError):
    def __init__(self, reader_index: int, reason: str) -> None:
        super().__init__(f"reader[{reader_index}] probe failed: {reason}")
        self.reader_index = reader_index
        self.reason = reason


# ----------------------------------------------------------------------
# Constants & helpers
# ----------------------------------------------------------------------


_LITERAL_RE = re.compile(
    r"""
    '(?:''|[^'])*'           # 'string literals' with '' escape
    | "(?:""|[^"])*"         # "identifiers"
    | \bX'[0-9a-fA-F]+'      # X'hex' BLOB literals
    | \bNULL\b               # NULL keyword in literal context
    | (?<![A-Za-z_])-?\d+(?:\.\d+)?(?![A-Za-z_])   # numeric literals
    """,
    re.VERBOSE | re.IGNORECASE,
)


_CONSTRAINT_KIND_BY_CODE: dict[int, str] = {
    275: "check",  # SQLITE_CONSTRAINT_CHECK
    787: "fk",  # SQLITE_CONSTRAINT_FOREIGNKEY
    1299: "notnull",  # SQLITE_CONSTRAINT_NOTNULL
    1555: "primarykey",  # SQLITE_CONSTRAINT_PRIMARYKEY
    2067: "unique",  # SQLITE_CONSTRAINT_UNIQUE
}


_PRAGMA_NORMALIZED: dict[str, dict[str, int]] = {
    "foreign_keys": {"ON": 1, "OFF": 0},
    "synchronous": {"NORMAL": 1, "FULL": 2, "OFF": 0},
    "temp_store": {"MEMORY": 2, "FILE": 1, "DEFAULT": 0},
    "query_only": {"ON": 1, "OFF": 0},
}


def _pragma_value_matches(name: str, expected: Any, actual: Any) -> bool:
    """Tolerate SQLite's PRAGMA value normalization (e.g. ON -> 1, MEMORY -> 2)."""
    if actual == expected:
        return True
    norm = _PRAGMA_NORMALIZED.get(name)
    if norm is not None and isinstance(expected, str):
        norm_expected = norm.get(expected.upper())
        if norm_expected is not None and actual == norm_expected:
            return True
    # Numeric tolerance: cache_size / mmap_size / journal_size_limit /
    # busy_timeout get echoed as int.
    try:
        if int(expected) == int(actual):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _template_only(sql: str) -> str:
    """Replace literal values in SQL with '?' placeholders."""
    return _LITERAL_RE.sub("?", sql)


def _shape(params: Any) -> list[str] | dict[str, str]:
    """Return parameter type names only — never values."""
    if params is None:
        return []
    if isinstance(params, Mapping):
        return {str(k): type(v).__name__ for k, v in params.items()}
    if isinstance(params, (list, tuple)):
        return [type(p).__name__ for p in params]
    # Single non-iterable parameter (atypical)
    return [type(params).__name__]


def _classify_integrity_error(
    e: aiosqlite.IntegrityError,
) -> tuple[str, str | None, str | None]:
    code = getattr(e, "sqlite_errorcode", None)
    kind = _CONSTRAINT_KIND_BY_CODE.get(code, "unknown") if code is not None else "unknown"
    if kind == "unknown":
        # Fallback message inspection for older sqlite/aiosqlite without codes.
        msg = str(e).lower()
        if "unique" in msg:
            kind = "unique"
        elif "not null" in msg:
            kind = "notnull"
        elif "foreign key" in msg:
            kind = "fk"
        elif "check" in msg:
            kind = "check"
        elif "primary key" in msg:
            kind = "primarykey"
    m = re.search(r"constraint failed:\s+(\w+)\.(\w+)", str(e))
    table, column = (m.group(1), m.group(2)) if m else (None, None)
    return kind, table, column


def _log_bg_task_exception(task: asyncio.Task[Any]) -> None:
    """Surface unhandled exceptions from fire-and-forget pool tasks.

    Without this callback, a crash in `_replace_connection` or `_safe_close`
    would be silently swallowed (asyncio defaults). Replacements failing
    silently mask critical pool-degradation events from operator visibility.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error(
            "pool.background_task_failed",
            task_name=task.get_name(),
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _is_disk_io_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return (
        "disk i/o error" in msg
        or "disk image is malformed" in msg
        or ("database is locked" in msg and "io" in msg)
    )


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    keys = list(row.keys())
    return {k: row[k] for k in keys}


def _row_to_dataclass[T](cls: type[T], row: aiosqlite.Row | None) -> T | None:
    if row is None:
        return None
    field_names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    keys = list(row.keys())
    kwargs = {k: row[k] for k in keys if k in field_names}
    return cls(**kwargs)


def _wrap_aiosqlite_error(
    e: aiosqlite.Error,
    *,
    sql: str,
    params: Any,
    role: str,
) -> PoolError:
    """Categorize an aiosqlite error into the pool's exception hierarchy.

    Logs at the appropriate level. Returns the wrapped exception (caller raises).
    """
    sql_template = _template_only(sql)
    params_shape = _shape(params)

    if isinstance(e, aiosqlite.IntegrityError):
        kind, table, column = _classify_integrity_error(e)
        _log.error(
            "pool.integrity_violation",
            role=role,
            constraint_kind=kind,
            table=table,
            column=column,
            sql=sql_template,
            params=params_shape,
        )
        return IntegrityViolationError(constraint_kind=kind, table=table, column=column)

    if isinstance(e, aiosqlite.OperationalError):
        msg_lower = str(e).lower()
        if "disk i/o error" in msg_lower or "disk image is malformed" in msg_lower:
            _log.error(
                "pool.connection_lost",
                role=role,
                reason=str(e),
                sql=sql_template,
                params=params_shape,
            )
            return ConnectionLostError(role=role, original_error=str(e))
        if (
            "syntax error" in msg_lower
            or "no such table" in msg_lower
            or "no such column" in msg_lower
            or "incomplete input" in msg_lower
            or "near " in msg_lower
        ):
            _log.critical(
                "pool.query_syntax_error",
                role=role,
                reason=str(e),
                sql=sql_template,
                params=params_shape,
            )
            return QuerySyntaxError(str(e))
        if "database is locked" in msg_lower or "busy" in msg_lower:
            _log.error(
                "pool.write_conflict",
                role=role,
                reason=str(e),
                sql=sql_template,
                params=params_shape,
            )
            return WriteConflictError(kind="immediate")

    # Catch-all — log without raw values and return as PoolError.
    _log.error(
        "pool.query_failed",
        role=role,
        reason=str(e),
        type=type(e).__name__,
        sql=sql_template,
        params=params_shape,
    )
    return QueryError(str(e))


# ----------------------------------------------------------------------
# Transaction handles
# ----------------------------------------------------------------------


class ReadTx:
    """Read-only handle returned from pool.read_transaction()."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def read_one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> dict[str, Any] | None:
        try:
            async with self._conn.execute(sql, params) as cur:
                row = await cur.fetchone()
                return _row_to_dict(row)
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    async def read_all(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[dict[str, Any]]:
        try:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [r for r in (_row_to_dict(row) for row in rows) if r is not None]
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    async def read_one_as(
        self, cls: type[T], sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> T | None:
        try:
            async with self._conn.execute(sql, params) as cur:
                row = await cur.fetchone()
                return _row_to_dataclass(cls, row)
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    async def read_all_as(
        self, cls: type[T], sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[T]:
        try:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [
                    obj for obj in (_row_to_dataclass(cls, row) for row in rows) if obj is not None
                ]
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    async def read_stream(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._conn.execute(sql, params) as cur:
                async for row in cur:
                    d = _row_to_dict(row)
                    if d is not None:
                        yield d
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e


class WriteTx:
    """Write handle returned from pool.write_transaction().

    Auto-commits on context exit; auto-rollbacks on exception.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        try:
            cur = await self._conn.execute(sql, params)
            try:
                return cur.rowcount
            finally:
                await cur.close()
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="writer") from e

    async def execute_many(
        self, sql: str, params_seq: Iterable[Sequence[Any] | Mapping[str, Any]]
    ) -> int:
        try:
            cur = await self._conn.executemany(sql, params_seq)
            try:
                return cur.rowcount
            finally:
                await cur.close()
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params="<many>", role="writer") from e

    async def read_one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> dict[str, Any] | None:
        try:
            async with self._conn.execute(sql, params) as cur:
                row = await cur.fetchone()
                return _row_to_dict(row)
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="writer") from e

    async def read_all(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[dict[str, Any]]:
        try:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [r for r in (_row_to_dict(row) for row in rows) if r is not None]
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="writer") from e

    async def read_one_as(
        self, cls: type[T], sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> T | None:
        try:
            async with self._conn.execute(sql, params) as cur:
                row = await cur.fetchone()
                return _row_to_dataclass(cls, row)
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="writer") from e

    async def read_all_as(
        self, cls: type[T], sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[T]:
        try:
            async with self._conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [
                    obj for obj in (_row_to_dataclass(cls, row) for row in rows) if obj is not None
                ]
        except aiosqlite.Error as e:
            raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="writer") from e


# ----------------------------------------------------------------------
# Pool
# ----------------------------------------------------------------------


_StateLiteral = Literal["initializing", "ready", "closing", "closed"]


class _PoolCreator:
    """Hybrid awaitable + async-context-manager returned from Pool.create().

    Supports both:
      pool = await Pool.create(...)
      async with Pool.create(...) as pool: ...
    """

    def __init__(self, cls: type[Pool], kwargs: dict[str, Any]) -> None:
        self._cls = cls
        self._kwargs = kwargs
        self._pool: Pool | None = None

    def __await__(self) -> Generator[Any, None, Pool]:
        return self._cls._async_create(**self._kwargs).__await__()

    async def __aenter__(self) -> Pool:
        self._pool = await self._cls._async_create(**self._kwargs)
        return self._pool

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._pool is not None:
            await self._pool.close()


class Pool:
    """Async DB pool with hybrid topology (1 writer + N readers)."""

    def __init__(
        self,
        *,
        database_path: str | Path,
        readers_count: int,
        busy_timeout_ms: int,
        cache_size_kib: int,
        mmap_size_bytes: int,
        journal_size_limit_bytes: int,
    ) -> None:
        self._database_path = database_path
        self._readers_count = readers_count
        self._busy_timeout_ms = busy_timeout_ms
        self._cache_size_kib = cache_size_kib
        self._mmap_size_bytes = mmap_size_bytes
        self._journal_size_limit_bytes = journal_size_limit_bytes

        self._writer: aiosqlite.Connection | None = None
        self._writer_lock = asyncio.Lock()
        self._readers: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=readers_count)
        self._reader_pool: list[aiosqlite.Connection] = []  # for index lookup

        self._state: _StateLiteral = "initializing"
        self._state_lock = asyncio.Lock()

        self._writer_healthy = False
        self._reader_healthy: dict[int, bool] = {}
        self._replacement_count: dict[str, int] = {"writer": 0, "reader": 0}
        self._replacement_timestamps: dict[str, list[float]] = {
            "writer": [],
            "reader": [],
        }

        self._created_monotonic = time.monotonic()
        self._total_writes = 0
        self._total_reads = 0

        # Background fire-and-forget tasks (replacement, safe-close).
        # Held to prevent GC; cleaned up via add_done_callback.
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_bg(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(_log_bg_task_exception)
        return task

    # --- Construction --------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        database_path: str | Path,
        readers_count: int,
        busy_timeout_ms: int = 5000,
        cache_size_kib: int = 16384,
        mmap_size_bytes: int = 268_435_456,
        journal_size_limit_bytes: int = 67_108_864,
        skip_schema_verify: bool = False,
    ) -> _PoolCreator:
        """Construct a pool. Supports both `await Pool.create(...)` and
        `async with Pool.create(...) as pool:` forms."""
        return _PoolCreator(
            cls,
            kwargs={
                "database_path": database_path,
                "readers_count": readers_count,
                "busy_timeout_ms": busy_timeout_ms,
                "cache_size_kib": cache_size_kib,
                "mmap_size_bytes": mmap_size_bytes,
                "journal_size_limit_bytes": journal_size_limit_bytes,
                "skip_schema_verify": skip_schema_verify,
            },
        )

    @classmethod
    async def _async_create(
        cls,
        *,
        database_path: str | Path,
        readers_count: int,
        busy_timeout_ms: int,
        cache_size_kib: int,
        mmap_size_bytes: int,
        journal_size_limit_bytes: int,
        skip_schema_verify: bool,
    ) -> Pool:
        pool = cls(
            database_path=database_path,
            readers_count=readers_count,
            busy_timeout_ms=busy_timeout_ms,
            cache_size_kib=cache_size_kib,
            mmap_size_bytes=mmap_size_bytes,
            journal_size_limit_bytes=journal_size_limit_bytes,
        )
        try:
            pool._writer = await pool._open_connection(role="writer")
            pool._writer_healthy = True
            for idx in range(readers_count):
                reader = await pool._open_connection(role="reader")
                pool._reader_pool.append(reader)
                pool._reader_healthy[idx] = True
                await pool._readers.put(reader)

            if skip_schema_verify:
                _log.warning(
                    "pool.schema_verification_skipped",
                    caller="Pool.create",
                )
            else:
                # Must verify with a reader (writer would also work). The
                # reader has query_only=ON which is fine for SELECTs.
                from orchestrator.db import migrate as _migrate

                reader = await pool._readers.get()
                try:
                    await _migrate.verify_schema_current(reader)
                finally:
                    await pool._readers.put(reader)

            pool._state = "ready"
            _log.info(
                "pool.initialized",
                readers_count=readers_count,
                database_path=str(database_path),
            )
        except BaseException:
            # Roll back any opened connections before propagating.
            await pool._teardown_connections()
            pool._state = "closed"
            raise

        return pool

    async def _open_connection(self, role: Literal["writer", "reader"]) -> aiosqlite.Connection:
        path = str(self._database_path)
        # Detect URI form ("file:..."): aiosqlite passes uri=True automatically
        # on file: prefix in modern versions, but be explicit here.
        if path.startswith("file:"):
            conn = await aiosqlite.connect(path, uri=True)
        else:
            conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row

        pragmas: list[tuple[str, Any]] = [
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

        applied: dict[str, Any] = {}
        for name, expected in pragmas:
            # PRAGMA syntax doesn't accept `?` parameter binding for the pragma
            # name or value. `name` and `expected` come from the hardcoded
            # `pragmas` list above — never user input — so this is safe.
            # nosem: semgrep.no-f-string-sql
            set_cur = await conn.execute(f"PRAGMA {name} = {expected}")
            await set_cur.close()
            # nosem: semgrep.no-f-string-sql
            read_cur = await conn.execute(f"PRAGMA {name}")
            try:
                row = await read_cur.fetchone()
                actual = row[0] if row else None
            finally:
                await read_cur.close()
            applied[name] = actual
            if not _pragma_value_matches(name, expected, actual):
                _log.critical(
                    "pool.pragma_mismatch",
                    role=role,
                    pragma=name,
                    expected=str(expected),
                    actual=str(actual),
                )
                await conn.close()
                raise PoolInitError(
                    reason=(f"PRAGMA {name} verify failed: expected {expected!r}, got {actual!r}"),
                    role=role,
                )

        _log.info(
            "pool.connection_opened",
            role=role,
            pragmas_applied=applied,
        )
        return conn

    async def _teardown_connections(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                await self._writer.close()
            self._writer = None
        for r in self._reader_pool:
            with contextlib.suppress(Exception):
                await r.close()
        self._reader_pool.clear()
        # Drain queue
        while not self._readers.empty():
            try:
                self._readers.get_nowait()
            except asyncio.QueueEmpty:
                break

    # --- Context manager + close --------------------------------------

    async def __aenter__(self) -> Pool:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        async with self._state_lock:
            if self._state in ("closing", "closed"):
                return
            self._state = "closing"
        await self._teardown_connections()
        self._state = "closed"
        _log.info("pool.closed")

    # --- Connection checkout helpers ----------------------------------

    @asynccontextmanager
    async def _checkout_reader(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._state != "ready":
            raise PoolClosedError(state=self._state)
        reader = await self._readers.get()
        healthy_after = True
        try:
            yield reader
        except (aiosqlite.OperationalError, ConnectionLostError) as e:
            # ConnectionLostError = our wrapped form (raised after the helper
            # categorized a disk-I/O OperationalError); raw OperationalError
            # is what aiosqlite emits before any wrap (e.g. from the raw
            # acquire_reader() escape hatch).
            is_lost = isinstance(e, ConnectionLostError) or _is_disk_io_error(e)
            if is_lost:
                healthy_after = False
                idx = self._index_of_reader(reader)
                self._spawn_bg(
                    self._replace_connection(role="reader", reader_index=idx, old_conn=reader)
                )
            raise
        finally:
            if healthy_after:
                await self._readers.put(reader)

    @asynccontextmanager
    async def _checkout_writer(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._state != "ready":
            raise PoolClosedError(state=self._state)
        async with self._writer_lock:
            if self._writer is None:
                raise PoolClosedError(state="closed")
            try:
                yield self._writer
            except (aiosqlite.OperationalError, ConnectionLostError) as e:
                is_lost = isinstance(e, ConnectionLostError) or _is_disk_io_error(e)
                if is_lost:
                    self._spawn_bg(self._replace_connection(role="writer", old_conn=self._writer))
                raise

    def _index_of_reader(self, conn: aiosqlite.Connection) -> int:
        for i, r in enumerate(self._reader_pool):
            if r is conn:
                return i
        return -1

    # --- Replacement state machine ------------------------------------

    async def _replace_connection(
        self,
        *,
        role: Literal["writer", "reader"],
        reader_index: int | None = None,
        old_conn: aiosqlite.Connection,
    ) -> None:
        now = time.monotonic()
        self._replacement_timestamps[role].append(now)
        recent = [t for t in self._replacement_timestamps[role] if now - t < 60]
        self._replacement_timestamps[role] = recent

        # Mark old as unhealthy synchronously
        if role == "writer":
            self._writer_healthy = False
        else:
            if reader_index is None:
                raise PoolError("reader replacement requires reader_index")
            self._reader_healthy[reader_index] = False

        # Storm guard: > 3 replacements in 60s → degraded; refuse further
        if len(recent) > 3:
            _log.critical(
                "pool.replacement_storm",
                role=role,
                count_in_60s=len(recent),
            )
            return

        # Best-effort close in background
        self._spawn_bg(self._safe_close(old_conn, role=role))

        # Open replacement
        try:
            new_conn = await self._open_connection(role=role)
        except Exception as e:
            _log.critical(
                "pool.replacement_failed",
                role=role,
                reader_index=reader_index,
                reason=str(e),
            )
            return

        # Atomic swap
        if role == "writer":
            self._writer = new_conn
            self._writer_healthy = True
        else:
            if reader_index is None:
                raise PoolError("reader replacement requires reader_index")
            # Replace in reader_pool and add to queue
            self._reader_pool[reader_index] = new_conn
            await self._readers.put(new_conn)
            self._reader_healthy[reader_index] = True

        self._replacement_count[role] += 1
        _log.warning(
            "pool.connection_replaced",
            role=role,
            reader_index=reader_index,
            replacement_count=self._replacement_count[role],
        )

    async def _safe_close(self, conn: aiosqlite.Connection, *, role: str) -> None:
        try:
            await asyncio.wait_for(conn.close(), timeout=2.0)
        except (TimeoutError, Exception) as e:
            _log.warning(
                "pool.safe_close_failed",
                role=role,
                reason=str(e),
            )

    # --- Single-statement helpers -------------------------------------

    async def read_one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> dict[str, Any] | None:
        async with self._checkout_reader() as conn:
            try:
                cur = await conn.execute(sql, params)
                try:
                    row = await cur.fetchone()
                finally:
                    await cur.close()
                self._total_reads += 1
                return _row_to_dict(row)
            except aiosqlite.Error as e:
                raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    async def read_all(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[dict[str, Any]]:
        async with self._checkout_reader() as conn:
            try:
                cur = await conn.execute(sql, params)
                try:
                    rows = await cur.fetchall()
                finally:
                    await cur.close()
                self._total_reads += 1
                return [r for r in (_row_to_dict(row) for row in rows) if r is not None]
            except aiosqlite.Error as e:
                raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    async def read_one_as(
        self, cls: type[T], sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> T | None:
        async with self._checkout_reader() as conn:
            try:
                cur = await conn.execute(sql, params)
                try:
                    row = await cur.fetchone()
                finally:
                    await cur.close()
                self._total_reads += 1
                return _row_to_dataclass(cls, row)
            except aiosqlite.Error as e:
                raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    async def read_all_as(
        self, cls: type[T], sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[T]:
        async with self._checkout_reader() as conn:
            try:
                cur = await conn.execute(sql, params)
                try:
                    rows = await cur.fetchall()
                finally:
                    await cur.close()
                self._total_reads += 1
                return [
                    obj for obj in (_row_to_dataclass(cls, row) for row in rows) if obj is not None
                ]
            except aiosqlite.Error as e:
                raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    async def execute_write(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        async with self._checkout_writer() as conn:
            try:
                cur = await conn.execute(sql, params)
                try:
                    rowcount = cur.rowcount
                finally:
                    await cur.close()
                await conn.commit()
                self._total_writes += 1
                return rowcount
            except aiosqlite.Error as e:
                # Best-effort rollback; ignore if there's no active txn.
                with contextlib.suppress(Exception):
                    await conn.rollback()
                raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="writer") from e

    async def execute_many_write(
        self,
        sql: str,
        params_seq: Iterable[Sequence[Any] | Mapping[str, Any]],
    ) -> int:
        async with self._checkout_writer() as conn:
            try:
                cur = await conn.executemany(sql, params_seq)
                try:
                    rowcount = cur.rowcount
                finally:
                    await cur.close()
                await conn.commit()
                self._total_writes += 1
                return rowcount
            except aiosqlite.Error as e:
                with contextlib.suppress(Exception):
                    await conn.rollback()
                raise _wrap_aiosqlite_error(e, sql=sql, params="<many>", role="writer") from e

    async def read_stream(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._checkout_reader() as conn:
            try:
                async with conn.execute(sql, params) as cur:
                    async for row in cur:
                        d = _row_to_dict(row)
                        if d is not None:
                            yield d
            except aiosqlite.Error as e:
                raise _wrap_aiosqlite_error(e, sql=sql, params=params, role="reader") from e

    # --- Multi-statement transactions ---------------------------------

    @asynccontextmanager
    async def read_transaction(self) -> AsyncIterator[ReadTx]:
        async with self._checkout_reader() as conn:
            yield ReadTx(conn)

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[WriteTx]:
        async with self._checkout_writer() as conn:
            # nosem: semgrep.no-f-string-sql  hardcoded SQL control, no user input
            await conn.execute("BEGIN IMMEDIATE")
            tx = WriteTx(conn)
            try:
                yield tx
            except BaseException:
                with contextlib.suppress(Exception):
                    # nosem: semgrep.no-f-string-sql  hardcoded SQL control, no user input
                    await conn.execute("ROLLBACK")
                _log.warning(
                    "pool.transaction_rolled_back",
                    role="writer",
                )
                raise
            else:
                # nosem: semgrep.no-f-string-sql  hardcoded SQL control, no user input
                await conn.execute("COMMIT")

    # --- Raw acquire --------------------------------------------------

    @asynccontextmanager
    async def acquire_reader(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._checkout_reader() as conn:
            yield conn

    @asynccontextmanager
    async def acquire_writer(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._checkout_writer() as conn:
            yield conn

    # --- Health & schema ----------------------------------------------

    async def health_check(self) -> dict[str, Any]:
        async def probe(
            conn: aiosqlite.Connection, role: str, idx: int | None
        ) -> tuple[bool, str | None]:
            try:
                # nosem: semgrep.no-f-string-sql  hardcoded health probe, no user input
                await asyncio.wait_for(conn.execute("SELECT 1"), timeout=1.0)
                return (True, None)
            except (TimeoutError, aiosqlite.Error) as e:
                return (False, str(e))

        # Probe writer
        if self._writer is not None:
            writer_task = asyncio.create_task(probe(self._writer, "writer", None))
        else:
            writer_task = None

        # Snapshot reader connections (don't drain queue — use _reader_pool)
        reader_tasks = [
            asyncio.create_task(probe(r, "reader", i))
            for i, r in enumerate(self._reader_pool)
            if self._reader_healthy.get(i, False)
        ]

        results = await asyncio.gather(
            *([writer_task] if writer_task is not None else []),
            *reader_tasks,
            return_exceptions=True,
        )

        if writer_task is not None:
            writer_result = results[0]
            reader_results = results[1:]
            writer_healthy = writer_result[0] if isinstance(writer_result, tuple) else False
        else:
            writer_healthy = False
            reader_results = results

        readers_healthy = sum(1 for r in reader_results if isinstance(r, tuple) and r[0])

        return {
            "writer": {
                "healthy": writer_healthy,
                "replacements": self._replacement_count["writer"],
            },
            "readers": {
                "total": self._readers_count,
                "healthy": readers_healthy,
                "replacements": self._replacement_count["reader"],
            },
            "uptime_sec": int(time.monotonic() - self._created_monotonic),
        }

    async def schema_status(self) -> dict[str, Any]:
        from orchestrator.db import migrate as _migrate

        async with self._checkout_reader() as conn:
            applied_set = await _migrate._load_applied_ids_async(conn)
        available_set = _migrate._load_available_ids()
        applied = sorted(applied_set)
        available = sorted(available_set)
        pending = sorted(available_set - applied_set)
        unknown = sorted(applied_set - available_set)
        return {
            "applied": applied,
            "available": available,
            "pending": pending,
            "unknown": unknown,
            "current": not pending and not unknown,
        }


# ----------------------------------------------------------------------
# Module-level singleton
# ----------------------------------------------------------------------


_pool: Pool | None = None
_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


async def init_pool(*, verify_schema: bool = True) -> Pool:
    """Initialize the module-level pool singleton.

    Idempotent: subsequent calls return the existing instance.
    """
    global _pool
    async with _get_init_lock():
        if _pool is not None:
            return _pool
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
        _pool = pool
        return pool


def get_pool() -> Pool:
    """Return the module-level pool. Raises if uninitialized."""
    if _pool is None:
        raise PoolNotInitializedError("init_pool() has not been called")
    return _pool


async def reload_pool() -> Pool:
    """Close the existing pool and create a fresh one."""
    global _pool
    if _pool is not None:
        old = _pool
        _pool = None
        await old.close()
    return await init_pool()


async def close_pool() -> None:
    """Close the module-level pool. No-op if uninitialized."""
    global _pool
    if _pool is None:
        return
    old = _pool
    _pool = None
    try:
        await asyncio.wait_for(old.close(), timeout=30.0)
    except TimeoutError:
        _log.error("pool.close_timed_out", reason="close_pool() exceeded 30s")
        raise PoolError("close_pool() timed out after 30s") from None
