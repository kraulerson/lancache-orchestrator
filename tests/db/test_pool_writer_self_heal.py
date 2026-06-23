"""Regression tests for the SEV-3 writer self-heal gap (UAT-11 F-INT-2, #152).

Readers recover after a replacement storm (`_lost_reader_slots` + heal-on-
acquire), but the writer did not: when a writer replacement gives up — the
storm guard trips, or the replacement open itself fails — `self._writer` is
left pointing at the dead connection with `_writer_healthy=False`, and
`_checkout_writer` kept yielding it. Every write then failed until a process
restart (health_check live-probes the writer → 503 → HEALTHCHECK restart).

These tests assert the fixed contract, mirroring the reader heal:
  1. The writer heals on checkout once the underlying fault clears — writes
     recover WITHOUT a restart, and the pool reports healthy again.
  2. If the heal-open itself fails (persistent fault), a write raises
     `PoolError` (loud → 503) rather than yielding a dead/closed connection.

The outer `asyncio.wait_for(...)` is a HANG DETECTOR: a closed aiosqlite
connection's `execute` can block on its dead worker thread, so on the buggy
code the write would hang and `wait_for` raises `TimeoutError` instead of the
expected result, failing the test loudly.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from orchestrator.db.pool import ConnectionLostError, Pool, PoolError

if TYPE_CHECKING:
    from pathlib import Path

import asyncio

pytestmark = pytest.mark.asyncio


async def _kill_writer(pool: Pool, monkeypatch) -> None:
    """Drive the writer into the dead state: a failing write triggers a
    replacement whose open also fails, so `_safe_close` closes the old writer
    and nothing replaces it — `self._writer` is now a CLOSED connection with
    `_writer_healthy=False`."""

    async def always_io_error(self, sql, parameters=()):
        raise aiosqlite.OperationalError("disk I/O error")

    async def fail_open(role):
        raise aiosqlite.OperationalError("unable to open database file")

    monkeypatch.setattr(aiosqlite.Connection, "execute", always_io_error)
    monkeypatch.setattr(pool, "_open_connection", fail_open)

    with contextlib.suppress(aiosqlite.OperationalError, ConnectionLostError, PoolError):
        await pool.execute_write("CREATE TABLE IF NOT EXISTS _heal_probe (x INTEGER)")
    # Let the fire-and-forget replacement task close the old writer + give up.
    await asyncio.sleep(0.3)
    assert pool._writer_healthy is False, "precondition: writer must be marked dead"


async def test_writer_recovers_after_fault_clears(db_path: Path, monkeypatch):
    async with Pool.create(database_path=db_path, readers_count=2) as pool:
        await _kill_writer(pool, monkeypatch)

        # Fault clears: restore real execute + open. The next write must heal a
        # fresh writer on checkout and succeed — not yield the closed one.
        monkeypatch.undo()
        await asyncio.wait_for(
            pool.execute_write("CREATE TABLE IF NOT EXISTS _heal_probe (x INTEGER)"),
            timeout=5.0,
        )

        # The heal restored writer health (buggy code leaves it False forever,
        # so health_check stays 503).
        assert pool._writer_healthy is True

        # And the pool keeps writing afterwards.
        rowcount = await asyncio.wait_for(
            pool.execute_write("INSERT INTO _heal_probe (x) VALUES (1)"), timeout=5.0
        )
        assert rowcount == 1


async def test_writer_raises_poolerror_when_heal_open_fails(db_path: Path, monkeypatch):
    async with Pool.create(database_path=db_path, readers_count=2) as pool:
        await _kill_writer(pool, monkeypatch)

        # Execute recovers but opening a replacement still fails: the writer can't
        # be healed, so a write must surface a PoolError (loud) — never yield the
        # dead/closed connection or hang.
        monkeypatch.setattr(aiosqlite.Connection, "execute", aiosqlite.Connection.execute)

        async def still_fail_open(role):
            raise aiosqlite.OperationalError("unable to open database file")

        monkeypatch.setattr(pool, "_open_connection", still_fail_open)

        with pytest.raises(PoolError):
            await asyncio.wait_for(
                pool.execute_write("INSERT INTO _heal_probe (x) VALUES (1)"), timeout=5.0
            )


async def test_write_transaction_commit_failure_not_wedged(db_path: Path, monkeypatch):
    """CORE-1 (review 2026-06-23): a COMMIT failure in `write_transaction()` must
    roll back the open transaction, not leave the single writer connection wedged
    mid-transaction. A non-disk-IO COMMIT error (e.g. SQLITE_BUSY) does NOT trip
    the writer-replacement path, so on the buggy code the connection stays
    `in_transaction=True` and the NEXT `write_transaction` fails its
    `BEGIN IMMEDIATE` with "cannot start a transaction within a transaction" —
    every subsequent write wedged until a process restart."""
    async with Pool.create(database_path=db_path, readers_count=2) as pool:
        await pool.execute_write("CREATE TABLE t (x INTEGER)")

        real_execute = aiosqlite.Connection.execute
        fail = {"on": True}

        async def fail_on_commit(self, sql, parameters=()):
            if fail["on"] and sql.strip().upper().startswith("COMMIT"):
                # SQLITE_BUSY — an OperationalError that is NOT a disk-IO error,
                # so it doesn't trigger writer replacement; exercises the wedge.
                raise aiosqlite.OperationalError("database is locked")
            return await real_execute(self, sql, parameters)

        monkeypatch.setattr(aiosqlite.Connection, "execute", fail_on_commit)

        # The COMMIT fails → the write_transaction must raise...
        with pytest.raises(aiosqlite.OperationalError):
            async with pool.write_transaction() as tx:
                await tx.execute("INSERT INTO t (x) VALUES (1)")

        # ...the writer must have been rolled back, not left mid-transaction.
        assert pool._writer is not None and not pool._writer.in_transaction

        # The fault clears; the NEXT write_transaction must succeed (buggy code:
        # BEGIN IMMEDIATE raises "cannot start a transaction within a transaction").
        fail["on"] = False
        async with asyncio.timeout(5.0):
            async with pool.write_transaction() as tx:
                await tx.execute("INSERT INTO t (x) VALUES (2)")

        rows = await pool.read_all("SELECT x FROM t ORDER BY x")
        # The failed-commit insert (1) was rolled back; only (2) survives.
        assert [r["x"] for r in rows] == [2]
