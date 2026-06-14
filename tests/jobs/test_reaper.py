"""Tests for orchestrator.jobs.reaper (ID6 startup job reaper)."""

from __future__ import annotations

import pytest

from orchestrator.jobs.reaper import REAPER_ERROR_MESSAGE, reap_running_jobs

pytestmark = pytest.mark.asyncio


# Use a non-singleton kind: migration 0004 permits only ONE in-flight
# library_sync per platform, but these reaper tests seed several concurrent
# in-flight jobs. prefill (per-game) has no such constraint and the reaper is
# kind-agnostic, so it is the realistic fixture for "multiple running jobs".
async def _insert_job(pool, kind="prefill", state="queued", started_at=None):
    """Insert a job row with the given state. Returns the new id."""
    sql = "INSERT INTO jobs (kind, platform, state, source, started_at) VALUES (?, ?, ?, 'api', ?)"
    await pool.execute_write(sql, (kind, "steam", state, started_at))
    row = await pool.read_one("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
    return row["id"]


class TestReaper:
    async def test_empty_jobs_table_returns_zero(self, pool):
        assert await reap_running_jobs(pool) == 0

    async def test_no_running_jobs_returns_zero(self, pool):
        # Seed queued + succeeded + failed; none of these should be touched.
        await _insert_job(pool, state="queued")
        await _insert_job(pool, state="succeeded", started_at="2026-05-27 10:00:00")
        await _insert_job(pool, state="failed", started_at="2026-05-27 10:00:00")
        await _insert_job(pool, state="cancelled", started_at="2026-05-27 10:00:00")

        n = await reap_running_jobs(pool)
        assert n == 0
        # Other states unchanged
        rows = await pool.read_all("SELECT state FROM jobs ORDER BY id")
        assert [r["state"] for r in rows] == [
            "queued",
            "succeeded",
            "failed",
            "cancelled",
        ]

    async def test_single_running_job_is_reaped(self, pool):
        job_id = await _insert_job(pool, state="running", started_at="2026-05-27 09:59:00")
        n = await reap_running_jobs(pool)
        assert n == 1

        row = await pool.read_one(
            "SELECT state, error, finished_at, started_at FROM jobs WHERE id=?",
            (job_id,),
        )
        assert row["state"] == "failed"
        assert row["error"] == REAPER_ERROR_MESSAGE
        assert row["finished_at"] is not None
        assert row["started_at"] == "2026-05-27 09:59:00"  # preserved

    async def test_multiple_running_jobs_all_reaped(self, pool):
        ids = []
        for _ in range(5):
            ids.append(await _insert_job(pool, state="running", started_at="2026-05-27 09:59:00"))
        n = await reap_running_jobs(pool)
        assert n == 5

        rows = await pool.read_all("SELECT id, state, error FROM jobs ORDER BY id")
        for row in rows:
            assert row["state"] == "failed"
            assert row["error"] == REAPER_ERROR_MESSAGE

    async def test_mixed_states_only_running_reaped(self, pool):
        await _insert_job(pool, state="queued")
        await _insert_job(pool, state="running", started_at="2026-05-27 09:59:00")
        await _insert_job(pool, state="succeeded", started_at="2026-05-27 09:55:00")
        await _insert_job(pool, state="running", started_at="2026-05-27 09:58:00")
        await _insert_job(pool, state="cancelled")

        n = await reap_running_jobs(pool)
        assert n == 2

        rows = await pool.read_all("SELECT state FROM jobs ORDER BY id")
        # original: queued, running, succeeded, running, cancelled
        # after:    queued, failed,  succeeded, failed,  cancelled
        assert [r["state"] for r in rows] == [
            "queued",
            "failed",
            "succeeded",
            "failed",
            "cancelled",
        ]

    async def test_idempotent(self, pool):
        """Second invocation reaps nothing (already-failed jobs are not
        re-touched)."""
        await _insert_job(pool, state="running", started_at="2026-05-27 09:59:00")
        assert await reap_running_jobs(pool) == 1
        assert await reap_running_jobs(pool) == 0

    async def test_error_message_truncates_under_jobs_constraint(self, pool):
        """Defensive: REAPER_ERROR_MESSAGE must fit within the orchestrator's
        own 200-char truncation convention (jobs.error). The constant
        itself is well under, but assert the contract."""
        assert len(REAPER_ERROR_MESSAGE) <= 200


async def _insert_game(pool, *, status="downloading", app_id="1"):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, status) VALUES ('steam', ?, 'G', ?)",
        (app_id, status),
    )
    row = await pool.read_one("SELECT id FROM games ORDER BY id DESC LIMIT 1")
    return row["id"]


class TestGameStatusReaper:
    """UAT-11 F-INT-1: a prefill cancelled by the per-job timeout (CancelledError
    bypasses the handler's `except Exception` reset) or killed by a crash leaves
    the game stuck 'downloading' forever. The boot reaper recovers it."""

    async def test_reaps_orphaned_downloading_game(self, pool):
        from orchestrator.jobs.reaper import reap_orphaned_game_status

        gid = await _insert_game(pool, status="downloading")
        n = await reap_orphaned_game_status(pool)
        assert n == 1
        row = await pool.read_one("SELECT status, last_error FROM games WHERE id=?", (gid,))
        assert row["status"] == "failed"
        assert row["last_error"]

    async def test_leaves_non_transient_statuses_untouched(self, pool):
        from orchestrator.jobs.reaper import reap_orphaned_game_status

        gid = await _insert_game(pool, status="up_to_date", app_id="2")
        n = await reap_orphaned_game_status(pool)
        assert n == 0
        row = await pool.read_one("SELECT status FROM games WHERE id=?", (gid,))
        assert row["status"] == "up_to_date"
