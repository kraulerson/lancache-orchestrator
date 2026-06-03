"""Regression tests for the SEV-2 reader-exhaustion deadlock (code review 2026-06-02).

Bug: when reader I/O errors drive connection replacement and the replacement
gives up (storm guard, or the replacement open itself fails), the reader is
pulled from the queue but never put back — capacity shrinks permanently. With
`_checkout_reader` awaiting `_readers.get()` with no timeout, once the queue
drains EVERY read blocks forever, while the pool still reports state="ready"
and raises no PoolError.

These tests assert the fixed contract:
  1. Reader exhaustion raises `PoolError` within a bounded time — never hangs.
  2. The pool recovers reader capacity once the underlying fault clears.

The outer `asyncio.wait_for(...)` in each test is a HANG DETECTOR: on the
buggy code the read blocks forever and `wait_for` raises `TimeoutError`
instead of the expected `PoolError`, failing the test loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from orchestrator.db.pool import ConnectionLostError, Pool, PoolError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


async def _drain_all_readers(pool: Pool, monkeypatch) -> None:
    """Force every reader to fail + every replacement-open to fail, so all
    reader slots leak out of the queue. Leaves `execute` patched to error."""

    async def always_io_error(self, sql, parameters=()):
        raise aiosqlite.OperationalError("disk I/O error")

    monkeypatch.setattr(aiosqlite.Connection, "execute", always_io_error)

    # One failing read per reader drains that slot (the failed background
    # replacement gives up and never re-queues). A couple extra for safety.
    for _ in range(pool._readers_count + 2):
        with contextlib.suppress(aiosqlite.OperationalError, ConnectionLostError, PoolError):
            await pool.read_one("SELECT 1")
    # Let the fire-and-forget replacement tasks finish failing.
    await asyncio.sleep(0.3)


async def test_reader_exhaustion_raises_poolerror_not_deadlock(db_path: Path, monkeypatch):
    async with Pool.create(
        database_path=db_path,
        readers_count=2,
        reader_acquire_timeout_sec=0.3,
    ) as pool:
        await _drain_all_readers(pool, monkeypatch)

        # The queue is now empty and every replacement open still fails, so a
        # read must surface a PoolError (loud) rather than blocking forever.
        with pytest.raises(PoolError):
            await asyncio.wait_for(pool.read_one("SELECT 1"), timeout=5.0)


async def test_reader_pool_recovers_after_fault_clears(db_path: Path, monkeypatch):
    async with Pool.create(
        database_path=db_path,
        readers_count=2,
        reader_acquire_timeout_sec=0.3,
    ) as pool:
        await _drain_all_readers(pool, monkeypatch)

        # Fault clears: restore real execute. The next read must heal a reader
        # slot on the acquire-timeout path and succeed.
        monkeypatch.undo()
        row = await asyncio.wait_for(pool.read_one("SELECT 1 AS one"), timeout=5.0)
        assert row is not None and row["one"] == 1

        # And the pool keeps serving afterwards.
        row2 = await asyncio.wait_for(pool.read_one("SELECT 2 AS two"), timeout=5.0)
        assert row2["two"] == 2
