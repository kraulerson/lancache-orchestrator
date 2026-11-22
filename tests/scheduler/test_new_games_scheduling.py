"""Scheduling of new_games_check: enqueue dedup + SchedulerManager registration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.scheduler.jobs import enqueue_new_games_check
from orchestrator.scheduler.manager import (
    NEW_GAMES_CHECK_JOB_ID,
    STEAM_DOWNLOAD_WATCH_JOB_ID,
    SchedulerManager,
)

pytestmark = pytest.mark.asyncio


async def test_enqueue_dedups(pool):
    assert await enqueue_new_games_check(pool) == 1
    assert await enqueue_new_games_check(pool, source="trigger") == 0
    rows = await pool.read_all("SELECT kind, source, state FROM jobs WHERE kind='new_games_check'")
    assert rows == [{"kind": "new_games_check", "source": "scheduler", "state": "queued"}]


async def test_enqueue_never_raises(pool):
    from orchestrator.db.pool import PoolError

    pool.execute_write = AsyncMock(side_effect=PoolError("boom"))
    assert await enqueue_new_games_check(pool) == 0


async def _sources(pool) -> list[tuple[str, str]]:
    rows = await pool.read_all(
        "SELECT state, source FROM jobs WHERE kind='new_games_check' ORDER BY id"
    )
    return [(r["state"], r["source"]) for r in rows]


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_scheduler_dedup_promotes_inflight_trigger_row(pool, state):
    """I1: a cron enqueue that dedups against a trigger/api row promotes that row's
    source, so the run still pushes the scheduler-only liveness heartbeat."""
    assert await enqueue_new_games_check(pool, source="trigger") == 1
    await pool.execute_write("UPDATE jobs SET state=? WHERE kind='new_games_check'", (state,))
    assert await enqueue_new_games_check(pool) == 0
    assert await _sources(pool) == [(state, "scheduler")]


async def test_non_scheduler_dedup_does_not_change_source(pool):
    assert await enqueue_new_games_check(pool, source="trigger") == 1
    assert await enqueue_new_games_check(pool, source="api") == 0
    assert await _sources(pool) == [("queued", "trigger")]


async def test_scheduler_dedup_never_promotes_finished_rows(pool):
    await pool.execute_write(
        "INSERT INTO jobs (kind, state, source) VALUES ('new_games_check', 'succeeded', 'api')"
    )
    assert await enqueue_new_games_check(pool, source="trigger") == 1
    assert await enqueue_new_games_check(pool) == 0
    assert await _sources(pool) == [("succeeded", "api"), ("queued", "scheduler")]


async def test_promote_failure_is_logged_and_never_raises(pool, monkeypatch):
    from structlog.testing import CapturingLogger

    import orchestrator.scheduler.jobs as jobs_mod
    from orchestrator.db.pool import PoolError

    # Patch the module logger: capture_logs is order-dependent here because the app
    # caches the bound logger on first use (see tests/scheduler/test_manager.py).
    rec = CapturingLogger()
    monkeypatch.setattr(jobs_mod, "_log", rec)
    pool.execute_write = AsyncMock(side_effect=[0, PoolError("db locked")])
    assert await enqueue_new_games_check(pool) == 0
    assert "scheduler.new_games_check.db_error" in [c.args[0] for c in rec.calls if c.args]


async def _ids(mgr: SchedulerManager) -> list[str]:
    await mgr.start()
    try:
        return mgr.get_registered_job_ids()
    finally:
        await mgr.shutdown()


async def test_manager_registers_cron_and_watch_jobs(pool):
    mgr = SchedulerManager(
        pool=pool,
        enabled=True,
        library_sync_interval_sec=21600,
        agent_client=AsyncMock(),
        steam_download_poll_sec=600,
    )
    ids = await _ids(mgr)
    assert NEW_GAMES_CHECK_JOB_ID in ids
    assert STEAM_DOWNLOAD_WATCH_JOB_ID in ids


async def test_manager_skips_watch_when_poll_zero_or_no_agent(pool):
    zero = SchedulerManager(
        pool=pool,
        enabled=True,
        library_sync_interval_sec=21600,
        agent_client=AsyncMock(),
        steam_download_poll_sec=0,
    )
    ids = await _ids(zero)
    assert NEW_GAMES_CHECK_JOB_ID in ids
    assert STEAM_DOWNLOAD_WATCH_JOB_ID not in ids

    no_agent = SchedulerManager(
        pool=pool,
        enabled=True,
        library_sync_interval_sec=21600,
        agent_client=None,
    )
    assert STEAM_DOWNLOAD_WATCH_JOB_ID not in await _ids(no_agent)


async def test_manager_skips_cron_when_disabled(pool):
    mgr = SchedulerManager(
        pool=pool,
        enabled=True,
        library_sync_interval_sec=21600,
        new_games_check_enabled=False,
    )
    assert NEW_GAMES_CHECK_JOB_ID not in await _ids(mgr)


async def test_manager_skips_watch_when_check_disabled(pool):
    """M4: the T1 watch only enqueues checks, so it must not run when checks are off."""
    mgr = SchedulerManager(
        pool=pool,
        enabled=True,
        library_sync_interval_sec=21600,
        agent_client=AsyncMock(),
        steam_download_poll_sec=600,
        new_games_check_enabled=False,
    )
    assert STEAM_DOWNLOAD_WATCH_JOB_ID not in await _ids(mgr)
