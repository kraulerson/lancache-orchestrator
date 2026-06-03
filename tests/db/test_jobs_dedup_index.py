"""SEV-3 (code review 2026-06-02): DB-enforced dedup for in-flight library_sync.

The app-level SELECT-then-INSERT dedup in `scheduler/jobs.py` and
`routers/sync.py` straddles an await and races onto duplicate in-flight rows on
concurrent cron+API triggers (the non-unique `idx_jobs_dedupe` does not prevent
it). Migration 0004 adds a partial UNIQUE index so at most one queued/running
`library_sync` per platform can exist, and the call sites use
`ON CONFLICT DO NOTHING`.
"""

from __future__ import annotations

import pytest

from orchestrator.db.pool import IntegrityViolationError

pytestmark = pytest.mark.asyncio

_INSERT = "INSERT INTO jobs (kind, platform, state, source) VALUES ('library_sync', 'steam', ?, ?)"


class TestLibrarySyncInflightUniqueIndex:
    async def test_on_conflict_second_inflight_insert_is_noop(self, pool):
        """A second in-flight library_sync/steam insert with ON CONFLICT
        DO NOTHING affects zero rows; exactly one in-flight row survives."""
        n1 = await pool.execute_write(_INSERT + " ON CONFLICT DO NOTHING", ("queued", "scheduler"))
        n2 = await pool.execute_write(_INSERT + " ON CONFLICT DO NOTHING", ("queued", "api"))
        assert n1 == 1
        assert n2 == 0
        rows = await pool.read_all(
            "SELECT id FROM jobs WHERE kind='library_sync' AND platform='steam' "
            "AND state IN ('queued','running')"
        )
        assert len(rows) == 1

    async def test_queued_then_running_same_platform_conflicts(self, pool):
        """queued + running both live in the partial index, so a running
        insert while one is queued is also deduped to a no-op."""
        await pool.execute_write(_INSERT + " ON CONFLICT DO NOTHING", ("queued", "scheduler"))
        n = await pool.execute_write(_INSERT + " ON CONFLICT DO NOTHING", ("running", "api"))
        assert n == 0

    async def test_raw_duplicate_insert_raises_integrity_error(self, pool):
        """Without ON CONFLICT, a duplicate in-flight insert hits the UNIQUE
        index and raises (proving the constraint is real, not just app-level)."""
        await pool.execute_write(_INSERT, ("queued", "scheduler"))
        with pytest.raises(IntegrityViolationError):
            await pool.execute_write(_INSERT, ("queued", "api"))

    async def test_other_platform_not_blocked(self, pool):
        """epic + steam in-flight library_sync coexist (per-platform uniqueness)."""
        await pool.execute_write(_INSERT, ("queued", "scheduler"))
        n = await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('library_sync', 'epic', 'queued', 'scheduler') ON CONFLICT DO NOTHING"
        )
        assert n == 1

    async def test_terminal_states_not_blocked(self, pool):
        """A succeeded/failed/cancelled library_sync is outside the partial
        index, so a fresh queued insert is allowed."""
        for state in ("succeeded", "failed", "cancelled"):
            await pool.execute_write(
                "INSERT INTO jobs (kind, platform, state, source, started_at, finished_at) "
                "VALUES ('library_sync', 'steam', ?, 'scheduler', "
                "'2026-06-02 09:00:00', '2026-06-02 09:05:00')",
                (state,),
            )
        n = await pool.execute_write(_INSERT + " ON CONFLICT DO NOTHING", ("queued", "scheduler"))
        assert n == 1
