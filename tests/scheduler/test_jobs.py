"""Tests for orchestrator.scheduler.jobs (F12)."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.scheduler.jobs import enqueue_library_sync, enqueue_validation_sweep

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

    async def test_concurrent_enqueue_creates_single_row(self, pool):
        """SEV-3 (review 2026-06-02): concurrent cron + API enqueues must not
        race onto duplicate in-flight rows. With the DB-enforced ON CONFLICT
        the outcome is deterministic — exactly one row inserted regardless of
        interleaving, and the two callers return [1, 0]."""
        results = await asyncio.gather(
            enqueue_library_sync(pool),
            enqueue_library_sync(pool),
        )
        assert sorted(results) == [0, 1]
        rows = await pool.read_all(
            "SELECT id FROM jobs WHERE kind='library_sync' AND state IN ('queued','running')"
        )
        assert len(rows) == 1

    async def test_returns_zero_on_pool_error_without_raising(self, pool):
        """Defensive: scheduler callbacks must never raise (would put the
        scheduler in a degraded state)."""
        from unittest.mock import AsyncMock

        from orchestrator.db.pool import PoolError

        # Replace the write path with a raising stub (the handler now inserts
        # atomically via execute_write + ON CONFLICT — no pre-SELECT).
        broken_pool = pool
        broken_pool.execute_write = AsyncMock(side_effect=PoolError("simulated"))
        n = await enqueue_library_sync(broken_pool)
        assert n == 0  # logged + returned; did not raise


class TestEnqueueValidationSweep:
    async def test_inserts_one_sweep_row(self, pool):
        n = await enqueue_validation_sweep(pool)
        assert n == 1
        row = await pool.read_one(
            "SELECT kind, platform, state, source FROM jobs WHERE kind='sweep'"
        )
        assert row == {
            "kind": "sweep",
            "platform": None,  # sweep is not platform-scoped
            "state": "queued",
            "source": "scheduler",
        }

    async def test_dedup_skip_when_inflight(self, pool):
        assert await enqueue_validation_sweep(pool) == 1
        assert await enqueue_validation_sweep(pool) == 0  # one in-flight already
        rows = await pool.read_all(
            "SELECT id FROM jobs WHERE kind='sweep' AND state IN ('queued','running')"
        )
        assert len(rows) == 1

    async def test_concurrent_enqueue_creates_single_row(self, pool):
        results = await asyncio.gather(
            enqueue_validation_sweep(pool),
            enqueue_validation_sweep(pool),
        )
        assert sorted(results) == [0, 1]

    async def test_returns_zero_on_pool_error_without_raising(self, pool):
        from unittest.mock import AsyncMock

        from orchestrator.db.pool import PoolError

        pool.execute_write = AsyncMock(side_effect=PoolError("simulated"))
        assert await enqueue_validation_sweep(pool) == 0  # never raises

    async def test_enqueue_sweep_full_writes_payload(self, pool):
        """`full=True` carries `{"full": true}` on jobs.payload; explicit source."""
        n = await enqueue_validation_sweep(pool, full=True, source="api")
        assert n == 1
        row = await pool.read_one("SELECT payload, source FROM jobs WHERE kind='sweep'")
        assert row["payload"] == '{"full": true}'
        assert row["source"] == "api"

    async def test_enqueue_sweep_default_no_payload(self, pool):
        """The default (weekly-cron) sweep has a NULL payload and scheduler source."""
        await enqueue_validation_sweep(pool)
        row = await pool.read_one("SELECT payload, source FROM jobs WHERE kind='sweep'")
        assert row["payload"] is None
        assert row["source"] == "scheduler"


async def _seed_game(
    pool, app_id, *, owned=1, current="42", cached=None, status="up_to_date", platform="steam"
):
    await pool.execute_write(
        "INSERT INTO games "
        "(platform, app_id, title, owned, current_version, cached_version, status)"
        " VALUES (?, ?, 'G', ?, ?, ?, ?)",
        (platform, app_id, owned, current, cached, status),
    )


class TestEnqueueScheduledPrefill:
    async def test_enqueues_never_cached(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

        await _seed_game(pool, "1", current="42", cached=None)
        n = await enqueue_scheduled_prefill(pool)
        assert n == 1
        row = await pool.read_one("SELECT kind, platform, state, source FROM jobs LIMIT 1")
        assert (row["kind"], row["state"], row["source"]) == ("prefill", "queued", "scheduler")

    async def test_enqueues_when_version_diverged(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

        await _seed_game(pool, "1", current="42", cached="41")
        assert await enqueue_scheduled_prefill(pool) == 1

    async def test_enqueues_validation_failed(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

        await _seed_game(pool, "1", current="42", cached="42", status="validation_failed")
        assert await enqueue_scheduled_prefill(pool) == 1

    async def test_skips_up_to_date(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

        await _seed_game(pool, "1", current="42", cached="42", status="up_to_date")
        assert await enqueue_scheduled_prefill(pool) == 0

    async def test_skips_unowned(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

        await _seed_game(pool, "1", owned=0, current="42", cached=None)
        assert await enqueue_scheduled_prefill(pool) == 0

    async def test_skips_blocked(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

        await _seed_game(pool, "1", current="42", cached=None)
        await pool.execute_write(
            "INSERT INTO block_list (platform, app_id, source) VALUES ('steam','1','api')"
        )
        assert await enqueue_scheduled_prefill(pool) == 0

    async def test_dedups_inflight_prefill(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

        await _seed_game(pool, "1", current="42", cached=None)
        gid = (await pool.read_one("SELECT id FROM games LIMIT 1"))["id"]
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source)"
            " VALUES ('prefill', ?, 'steam', 'queued', 'api')",
            (gid,),
        )
        assert await enqueue_scheduled_prefill(pool) == 0
