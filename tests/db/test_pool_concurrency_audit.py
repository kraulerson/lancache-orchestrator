"""Regression tests for the pool concurrency findings from the full-codebase
audit (2026-06-09).

1. SEV-2 — reader heal is an unsynchronized check-then-act: under a reader
   deficit + concurrent acquirers, two coroutines both pass the
   ``_lost_reader_slots > 0`` guard before either decrements, both mint a
   reader, over-heal past ``readers_count``, drive the deficit negative, and the
   surplus reader's release ``put()`` into the bounded queue blocks forever.

2. SEV-2 consequence — the release path must never block forever on a full
   reader queue (defense in depth for the over-heal overflow).

3. SEV-4 — two concurrent writer replacements both assign ``self._writer``; the
   first new connection is overwritten and leaked (never closed).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from orchestrator.db.pool import Pool, PoolError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


async def test_concurrent_acquire_does_not_overheal_reader_slots(
    db_path: Path, monkeypatch
) -> None:
    """Deficit of 1 + two concurrent reads → at most ONE heal. The second read
    must fail loudly (PoolError), not silently mint a surplus reader, and the
    deficit must never go negative."""
    async with Pool.create(
        database_path=db_path, readers_count=2, reader_acquire_timeout_sec=0.2
    ) as pool:
        # Precondition: both readers unavailable (queue drained) with exactly
        # one genuinely-lost slot recorded. Connections still open fine, so a
        # heal can proceed.
        while not pool._readers.empty():
            pool._readers.get_nowait()
        pool._lost_reader_slots = 1
        pool._reader_healthy = {0: True, 1: False}

        results = await asyncio.wait_for(
            asyncio.gather(
                pool.read_one("SELECT 1"),
                pool.read_one("SELECT 1"),
                return_exceptions=True,
            ),
            timeout=8.0,
        )

        poolerrors = [r for r in results if isinstance(r, PoolError)]
        # deficit was 1 → only one heal is allowed; the other acquirer must
        # surface a PoolError rather than over-healing.
        assert len(poolerrors) == 1, results
        # The double-heal bug drives the deficit to -1, which then suppresses
        # all future legitimate heals.
        assert pool._lost_reader_slots == 0


async def test_reader_release_never_blocks_on_full_queue(db_path: Path) -> None:
    """Releasing a reader when the queue is unexpectedly full must not block
    forever — the over-heal overflow consequence."""
    async with Pool.create(database_path=db_path, readers_count=1) as pool:
        cm = pool._checkout_reader()
        await cm.__aenter__()  # the single reader is out; queue empty
        filler = await pool._open_connection(role="reader")
        pool._readers.put_nowait(filler)  # queue now full (maxsize=1)
        assert pool._readers.full()

        # On the buggy `await self._readers.put(reader)` this hangs forever and
        # wait_for raises TimeoutError; the fixed put_nowait+close-surplus path
        # completes promptly.
        await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=3.0)


async def test_concurrent_writer_replacement_closes_surplus(db_path: Path, monkeypatch) -> None:
    """Two concurrent writer replacements for the same broken writer must
    install exactly one new connection and CLOSE the surplus — never leak it."""
    async with Pool.create(database_path=db_path, readers_count=1) as pool:
        old = pool._writer

        opened: list = []
        real_open = pool._open_connection

        async def tracking_open(role):
            conn = await real_open(role)
            opened.append(conn)
            return conn

        monkeypatch.setattr(pool, "_open_connection", tracking_open)

        closed: list = []
        real_close = pool._safe_close

        async def tracking_close(conn, *, role):
            closed.append(conn)
            await real_close(conn, role=role)

        monkeypatch.setattr(pool, "_safe_close", tracking_close)

        await asyncio.gather(
            pool._replace_connection(role="writer", old_conn=old),
            pool._replace_connection(role="writer", old_conn=old),
        )

        assert pool._writer in opened
        surplus = [c for c in opened if c is not pool._writer]
        assert len(surplus) == 1
        # The loser must be closed, not leaked (buggy code never closes it).
        assert surplus[0] in closed
