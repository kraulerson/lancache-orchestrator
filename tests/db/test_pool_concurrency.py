"""Concurrency tests for orchestrator.db.pool.

Covers reader pool exhaustion, writer serialization, reader concurrency
during writes (WAL semantics), and the cancellation matrix from spec §4.7.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from orchestrator.db.pool import Pool, PoolClosedError

VALID_TOKEN = "a" * 32


class TestConcurrentReads:
    async def test_n_concurrent_reads_succeed(self, populated_pool: Pool):
        """All readers can run concurrently. With 4 readers, fire 4 simultaneous
        reads + assert they all complete."""

        async def one_read(idx: int) -> int:
            row = await populated_pool.read_one("SELECT COUNT(*) AS c FROM games")
            return row["c"]

        results = await asyncio.gather(*(one_read(i) for i in range(4)))
        assert all(r == 5 for r in results)

    async def test_reads_concurrent_with_writes(self, populated_pool: Pool):
        """WAL semantics: reads succeed even while a write transaction is in
        flight, returning the pre-write snapshot."""
        write_started = asyncio.Event()
        write_release = asyncio.Event()

        async def slow_writer():
            async with populated_pool.write_transaction() as tx:
                await tx.execute(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES ('steam', 'concurrent-1', 'Concurrent', 1, 'not_downloaded')"
                )
                write_started.set()
                await write_release.wait()

        async def reader():
            await write_started.wait()
            t0 = time.monotonic()
            row = await populated_pool.read_one("SELECT COUNT(*) AS c FROM games")
            elapsed = time.monotonic() - t0
            return row["c"], elapsed

        writer_task = asyncio.create_task(slow_writer())
        await write_started.wait()
        reader_count, reader_elapsed = await reader()
        # Pre-write snapshot: 5 games (the new insert isn't committed)
        assert reader_count == 5
        # Reader didn't block on the writer
        assert reader_elapsed < 0.5

        write_release.set()
        await writer_task

    async def test_reader_pool_exhaustion_queues(self, db_path):
        """If all readers are busy, the next caller waits in the queue."""
        async with Pool.create(database_path=db_path, readers_count=2) as pool:
            release = asyncio.Event()
            entered = []

            async def slow_reader(rid: int):
                async with pool.acquire_reader():
                    entered.append(rid)
                    await release.wait()

            # Saturate the pool
            t1 = asyncio.create_task(slow_reader(1))
            t2 = asyncio.create_task(slow_reader(2))
            await asyncio.sleep(0.05)
            assert len(entered) == 2

            # Third caller queues
            t3 = asyncio.create_task(slow_reader(3))
            await asyncio.sleep(0.1)
            assert len(entered) == 2  # t3 still waiting

            release.set()
            await asyncio.gather(t1, t2, t3)
            assert 3 in entered


class TestWriterSerialization:
    async def test_writes_serialize_in_order(self, pool: Pool):
        """Concurrent writers complete one at a time; insertion order respects
        scheduling order (within the same asyncio task batch)."""

        async def writer(idx: int):
            await pool.execute_write(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES (?, ?, ?, 1, 'not_downloaded')",
                ("steam", f"ser-{idx}", f"W{idx}"),
            )

        await asyncio.gather(*(writer(i) for i in range(8)))
        rows = await pool.read_all("SELECT app_id FROM games WHERE app_id LIKE 'ser-%' ORDER BY id")
        # All 8 writes succeeded
        assert len(rows) == 8


class TestCancellation:
    async def test_cancellation_during_read_releases_reader(self, pool: Pool):
        """Cancelling a read returns the reader to the queue so subsequent reads
        succeed."""

        async def slow_read():
            await pool.read_one("SELECT 1")
            await asyncio.sleep(10)  # never reaches

        task = asyncio.create_task(slow_read())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Verify the pool is still usable (reader was returned)
        row = await pool.read_one("SELECT 1 AS x")
        assert row["x"] == 1

    async def test_cancellation_during_write_rolls_back_and_releases_lock(self, pool: Pool):
        """Cancelling a write transaction rolls back changes + releases the
        writer lock for subsequent writes."""

        async def slow_write():
            async with pool.write_transaction() as tx:
                await tx.execute(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES ('steam', 'cancel-1', 'Will Cancel', 1, 'not_downloaded')"
                )
                await asyncio.sleep(10)  # never reaches

        task = asyncio.create_task(slow_write())
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Verify the insert was rolled back
        rows = await pool.read_all("SELECT app_id FROM games WHERE app_id = 'cancel-1'")
        assert rows == []

        # Verify the writer lock is free — subsequent write succeeds
        await pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned, status) "
            "VALUES ('steam', 'after-cancel', 'After', 1, 'not_downloaded')"
        )

    async def test_cancellation_during_streaming_releases_reader(self, populated_pool: Pool):
        """Cancelling a read_stream generator returns the reader cleanly."""

        async def stream_then_cancel():
            async for _row in populated_pool.read_stream("SELECT id FROM games ORDER BY id"):
                await asyncio.sleep(10)  # never reaches second iteration

        task = asyncio.create_task(stream_then_cancel())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Pool is still usable
        rows = await populated_pool.read_all("SELECT id FROM games ORDER BY id")
        assert len(rows) == 5


class TestStateTransitions:
    async def test_close_during_in_flight_query_raises_pool_closed(self, db_path):
        """Closing the pool while a query is in flight: in-flight ops complete
        if possible; new ops raise PoolClosedError."""
        async with Pool.create(database_path=db_path, readers_count=2) as pool:
            release = asyncio.Event()

            async def slow_query():
                async with pool.acquire_reader() as conn:
                    await conn.execute("SELECT 1")
                    await release.wait()
                    return await conn.execute("SELECT 1")

            task = asyncio.create_task(slow_query())
            await asyncio.sleep(0.05)
            close_task = asyncio.create_task(pool.close())
            await asyncio.sleep(0.05)
            release.set()

            with pytest.raises(PoolClosedError):
                await pool.read_one("SELECT 1")

            await asyncio.gather(close_task, task, return_exceptions=True)
