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


_SWEEP = "INSERT INTO jobs (kind, state, source) VALUES ('sweep', ?, 'scheduler')"


class TestSweepInflightUniqueIndex:
    """F13: migration 0005 — at most one queued/running `sweep` job."""

    async def test_on_conflict_second_inflight_insert_is_noop(self, pool):
        n1 = await pool.execute_write(_SWEEP + " ON CONFLICT DO NOTHING", ("queued",))
        n2 = await pool.execute_write(_SWEEP + " ON CONFLICT DO NOTHING", ("queued",))
        assert n1 == 1
        assert n2 == 0
        rows = await pool.read_all(
            "SELECT id FROM jobs WHERE kind='sweep' AND state IN ('queued','running')"
        )
        assert len(rows) == 1

    async def test_queued_then_running_conflicts(self, pool):
        await pool.execute_write(_SWEEP + " ON CONFLICT DO NOTHING", ("queued",))
        n = await pool.execute_write(_SWEEP + " ON CONFLICT DO NOTHING", ("running",))
        assert n == 0

    async def test_raw_duplicate_insert_raises_integrity_error(self, pool):
        await pool.execute_write(_SWEEP, ("queued",))
        with pytest.raises(IntegrityViolationError):
            await pool.execute_write(_SWEEP, ("queued",))

    async def test_terminal_states_not_blocked(self, pool):
        for state in ("succeeded", "failed", "cancelled"):
            await pool.execute_write(
                "INSERT INTO jobs (kind, state, source, started_at, finished_at) "
                "VALUES ('sweep', ?, 'scheduler', "
                "'2026-06-07 03:00:00', '2026-06-07 03:05:00')",
                (state,),
            )
        n = await pool.execute_write(_SWEEP + " ON CONFLICT DO NOTHING", ("queued",))
        assert n == 1


# Audit 2026-06-09: migration 0006 — at most one queued/running prefill, and at
# most one queued/running validate, per game.


async def _make_game(pool, *, platform: str = "steam", app_id: str = "440") -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title) VALUES (?, ?, 'G')",
        (platform, app_id),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return int(row["id"])


_PREFILL = (
    "INSERT INTO jobs (kind, game_id, platform, state, source) "
    "VALUES ('prefill', ?, 'steam', ?, 'api')"
)


class TestPrefillInflightUniqueIndex:
    async def test_raw_duplicate_insert_raises_integrity_error(self, pool):
        gid = await _make_game(pool)
        await pool.execute_write(_PREFILL, (gid, "queued"))
        with pytest.raises(IntegrityViolationError):
            await pool.execute_write(_PREFILL, (gid, "queued"))

    async def test_on_conflict_second_inflight_insert_is_noop(self, pool):
        gid = await _make_game(pool)
        n1 = await pool.execute_write(_PREFILL + " ON CONFLICT DO NOTHING", (gid, "queued"))
        n2 = await pool.execute_write(_PREFILL + " ON CONFLICT DO NOTHING", (gid, "running"))
        assert n1 == 1
        assert n2 == 0

    async def test_different_games_not_blocked(self, pool):
        g1 = await _make_game(pool, app_id="1")
        g2 = await _make_game(pool, app_id="2")
        await pool.execute_write(_PREFILL, (g1, "queued"))
        n = await pool.execute_write(_PREFILL + " ON CONFLICT DO NOTHING", (g2, "queued"))
        assert n == 1

    async def test_terminal_states_not_blocked(self, pool):
        gid = await _make_game(pool)
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source, started_at, finished_at) "
            "VALUES ('prefill', ?, 'steam', 'succeeded', 'api', "
            "'2026-06-09 09:00:00', '2026-06-09 09:05:00')",
            (gid,),
        )
        n = await pool.execute_write(_PREFILL + " ON CONFLICT DO NOTHING", (gid, "queued"))
        assert n == 1


_VALIDATE = (
    "INSERT INTO jobs (kind, game_id, platform, state, source) "
    "VALUES ('validate', ?, 'steam', ?, 'api')"
)


class TestValidateInflightUniqueIndex:
    async def test_raw_duplicate_insert_raises_integrity_error(self, pool):
        gid = await _make_game(pool)
        await pool.execute_write(_VALIDATE, (gid, "queued"))
        with pytest.raises(IntegrityViolationError):
            await pool.execute_write(_VALIDATE, (gid, "queued"))

    async def test_on_conflict_second_inflight_insert_is_noop(self, pool):
        gid = await _make_game(pool)
        n1 = await pool.execute_write(_VALIDATE + " ON CONFLICT DO NOTHING", (gid, "queued"))
        n2 = await pool.execute_write(_VALIDATE + " ON CONFLICT DO NOTHING", (gid, "running"))
        assert n1 == 1
        assert n2 == 0
