"""Tests for orchestrator.scheduler.jobs (F12)."""

from __future__ import annotations

import pytest

from orchestrator.scheduler.jobs import enqueue_library_sync

pytestmark = pytest.mark.asyncio


class TestEnqueueLibrarySync:
    async def test_inserts_queued_job_when_table_empty(self, pool):
        n = await enqueue_library_sync(pool)
        assert n == 1
        row = await pool.read_one("SELECT kind, platform, state, source FROM jobs LIMIT 1")
        assert row == {
            "kind": "library_sync",
            "platform": "steam",
            "state": "queued",
            "source": "scheduler",
        }

    async def test_dedup_skip_when_queued_already(self, pool):
        # Seed a queued row from a previous schedule fire / manual trigger.
        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('library_sync', 'steam', 'queued', 'api')"
        )
        n = await enqueue_library_sync(pool)
        assert n == 0  # dedup hit; nothing inserted
        rows = await pool.read_all(
            "SELECT id FROM jobs WHERE kind='library_sync' AND state='queued'"
        )
        assert len(rows) == 1

    async def test_dedup_skip_when_running_already(self, pool):
        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source, started_at) "
            "VALUES ('library_sync', 'steam', 'running', 'scheduler', "
            "'2026-05-28 12:00:00')"
        )
        n = await enqueue_library_sync(pool)
        assert n == 0

    async def test_enqueues_when_only_terminal_states_exist(self, pool):
        # Past jobs in terminal states don't block a new schedule fire.
        for state in ("succeeded", "failed", "cancelled"):
            await pool.execute_write(
                "INSERT INTO jobs (kind, platform, state, source, "
                "started_at, finished_at) VALUES (?, 'steam', ?, "
                "'scheduler', '2026-05-28 09:00:00', '2026-05-28 09:05:00')",
                ("library_sync", state),
            )
        n = await enqueue_library_sync(pool)
        assert n == 1
        # Total rows: 3 terminal + 1 new queued = 4
        rows = await pool.read_all("SELECT id FROM jobs")
        assert len(rows) == 4

    async def test_dedup_ignores_other_kinds(self, pool):
        """A queued `validate` or `sweep` job must not block library_sync."""
        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('prefill', 'steam', 'queued', 'scheduler')"
        )
        n = await enqueue_library_sync(pool)
        assert n == 1

    async def test_dedup_ignores_other_platforms(self, pool):
        """An epic library_sync (when F2 ships) must not block steam."""
        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('library_sync', 'epic', 'queued', 'scheduler')"
        )
        n = await enqueue_library_sync(pool)
        assert n == 1

    async def test_returns_zero_on_pool_error_without_raising(self, pool):
        """Defensive: scheduler callbacks must never raise (would put the
        scheduler in a degraded state)."""
        from unittest.mock import AsyncMock

        from orchestrator.db.pool import PoolError

        # Replace pool methods with raising stubs.
        broken_pool = pool
        broken_pool.read_one = AsyncMock(side_effect=PoolError("simulated"))
        n = await enqueue_library_sync(broken_pool)
        assert n == 0  # logged + returned; did not raise
