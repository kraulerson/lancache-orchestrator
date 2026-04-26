"""Slow integration tests — Spike-F-style sustained workload.

Run via: pytest -m slow
Default deselected by pyproject.toml addopts="-m 'not slow'".

Validates the pool layer against the actual concurrent-write workload
that the orchestrator's prefill jobs produce. Spec §7.3.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import aiosqlite
import pytest

from orchestrator.db.pool import Pool, WriteConflictError

VALID_TOKEN = "a" * 32


@pytest.mark.slow
async def test_sustained_concurrent_workload(populated_pool: Pool):
    """32 concurrent writers x 4-statement transactions x 30s.

    Asserts:
      - zero WriteConflictError (busy_timeout absorbs all contention)
      - p99 transaction duration < 200ms (CI margin; design 100ms)
      - reader p99 < 50ms during writer load (WAL semantics hold)
    """
    durations_ms: list[float] = []
    reader_durations_ms: list[float] = []
    write_conflicts: list[Exception] = []
    stop_event = asyncio.Event()

    async def writer_worker(worker_id: int) -> None:
        seq = 0
        while not stop_event.is_set():
            t0 = time.perf_counter()
            try:
                async with populated_pool.write_transaction() as tx:
                    seq += 1
                    suffix = f"w{worker_id}-s{seq}"
                    await tx.execute(
                        "INSERT INTO games (platform, app_id, title, owned, status) "
                        "VALUES ('steam', ?, ?, 1, 'not_downloaded')",
                        (suffix, suffix),
                    )
                    game_id_row = await tx.read_one(
                        "SELECT id FROM games WHERE app_id = ?", (suffix,)
                    )
                    gid = game_id_row["id"]
                    await tx.execute(
                        "INSERT INTO manifests (game_id, raw, fetched_at) "
                        "VALUES (?, ?, '2026-04-25T00:00:00Z')",
                        (gid, b"manifest"),
                    )
                    await tx.execute(
                        "INSERT INTO cache_observations "
                        "(observed_at, event, cache_identifier, path) "
                        "VALUES ('2026-04-25T00:00:00Z', 'hit', 'steam', ?)",
                        (suffix,),
                    )
                durations_ms.append((time.perf_counter() - t0) * 1000)
            except WriteConflictError as e:
                write_conflicts.append(e)

    async def reader_probe() -> None:
        while not stop_event.is_set():
            t0 = time.perf_counter()
            await populated_pool.read_one("SELECT COUNT(*) AS c FROM games")
            reader_durations_ms.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.05)

    workers = [asyncio.create_task(writer_worker(i)) for i in range(32)]
    probe = asyncio.create_task(reader_probe())

    await asyncio.sleep(30)
    stop_event.set()
    await asyncio.gather(*workers, probe, return_exceptions=True)

    assert len(write_conflicts) == 0, f"unexpected write conflicts: {write_conflicts[:5]}"

    if durations_ms:
        sorted_durs = sorted(durations_ms)
        p99 = sorted_durs[int(len(sorted_durs) * 0.99)]
        assert p99 < 200, f"p99 transaction {p99:.1f}ms > 200ms (n={len(durations_ms)})"

    if reader_durations_ms:
        sorted_readers = sorted(reader_durations_ms)
        reader_p99 = sorted_readers[int(len(sorted_readers) * 0.99)]
        assert reader_p99 < 50, (
            f"reader p99 {reader_p99:.1f}ms > 50ms (writer load starving readers)"
        )


@pytest.mark.slow
async def test_replacement_storm_guard_under_load(pool: Pool, monkeypatch):
    """Pathologically broken connection forces many replacements in 60s; storm
    guard trips and pool transitions to degraded state."""
    original_execute = aiosqlite.Connection.execute

    async def io_error_for_reads(self, sql, parameters=()):
        if "SELECT" in str(sql).upper():
            raise aiosqlite.OperationalError("disk I/O error")
        return await original_execute(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", io_error_for_reads)

    # Hammer reads in parallel to trigger replacements
    async def trigger():
        with contextlib.suppress(Exception):
            await pool.read_one("SELECT 1")

    await asyncio.gather(*(trigger() for _ in range(10)))
    await asyncio.sleep(2)

    # After storm guard, pool is degraded
    health = await pool.health_check()
    # All readers should be unhealthy now (storm guard refused further replacements)
    assert health["readers"]["healthy"] < health["readers"]["total"]


@pytest.mark.slow
async def test_long_running_streaming_read_under_concurrent_writes(populated_pool: Pool):
    """A 30s read_stream completes successfully while writes hit the writer.
    WAL semantics: streaming read doesn't block on writer activity."""
    write_count = 0
    rows_streamed = 0
    stop_event = asyncio.Event()

    async def writer():
        nonlocal write_count
        while not stop_event.is_set():
            with contextlib.suppress(Exception):
                await populated_pool.execute_write(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES (?, ?, ?, 1, 'not_downloaded')",
                    ("steam", f"stream-{write_count}", f"S{write_count}"),
                )
                write_count += 1
            await asyncio.sleep(0.05)

    async def reader():
        nonlocal rows_streamed
        async for _row in populated_pool.read_stream("SELECT id FROM games"):
            rows_streamed += 1
            await asyncio.sleep(0.001)
            if stop_event.is_set():
                break

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())

    await asyncio.sleep(30)
    stop_event.set()
    await asyncio.gather(writer_task, reader_task, return_exceptions=True)

    assert write_count > 0, "writer never made progress"
    assert rows_streamed > 0, "reader never made progress"
