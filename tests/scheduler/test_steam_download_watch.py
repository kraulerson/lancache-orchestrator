"""SteamDownloadWatch (T1): enqueue a new_games_check when the downloaded record gains entries."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.scheduler.steam_download_watch import SteamDownloadWatch

pytestmark = pytest.mark.asyncio


def _watch(pool, *responses):
    agent = AsyncMock()
    agent.downloaded_state = AsyncMock(side_effect=list(responses))
    return SteamDownloadWatch(pool, agent)


async def _count(pool) -> int:
    row = await pool.read_one("SELECT COUNT(*) AS n FROM jobs WHERE kind='new_games_check'")
    return row["n"]


async def _clear(pool) -> None:
    await pool.execute_write("DELETE FROM jobs WHERE kind='new_games_check'")


async def test_watch_first_poll_sets_baseline_without_enqueue(pool):
    w = _watch(pool, {"1": [10, 11]})
    assert await w.poll() is False
    assert await _count(pool) == 0


async def test_watch_added_entry_enqueues_trigger(pool):
    w = _watch(pool, {"1": [10]}, {"1": [10, 11]}, {"1": [10, 11]})
    await w.poll()
    assert await w.poll() is True
    row = await pool.read_one("SELECT source, state FROM jobs WHERE kind='new_games_check'")
    assert row == {"source": "trigger", "state": "queued"}
    await _clear(pool)
    # baseline moved: same state again does not enqueue
    assert await w.poll() is False
    assert await _count(pool) == 0


async def test_watch_unchanged_or_removed_only_does_not_enqueue(pool):
    w = _watch(pool, {"1": [10, 11]}, {"1": [10, 11]}, {"1": [10]}, {"1": [10, 11]})
    await w.poll()
    assert await w.poll() is False  # unchanged
    assert await w.poll() is False  # removed-only; baseline moves to {10}
    assert await _count(pool) == 0
    # 11 is now ADDED relative to the moved baseline
    assert await w.poll() is True


async def test_watch_agent_error_or_empty_keeps_baseline(pool):
    w = _watch(pool, {"1": [10, 11]}, RuntimeError("agent down"), {}, {"1": [10, 11]})
    await w.poll()
    assert await w.poll() is False  # error
    assert await w.poll() is False  # empty
    assert await w.poll() is False  # original set back: no trigger
    assert await _count(pool) == 0


async def test_watch_dedup_keeps_baseline_and_retries(pool):
    """A deduped enqueue does NOT move the baseline: the in-flight check may have
    read its check set before the new download landed, so the next poll retries."""
    from orchestrator.scheduler.jobs import enqueue_new_games_check

    await enqueue_new_games_check(pool)  # a check already in flight
    w = _watch(pool, {"1": [10]}, {"1": [10, 11]}, {"1": [10, 11]}, {"1": [10, 11]})
    await w.poll()
    assert await w.poll() is False  # enqueue deduped (rowcount 0)
    await _clear(pool)  # the in-flight check finishes
    assert await w.poll() is True  # baseline kept: 11 is still "added", so it retries
    await _clear(pool)
    assert await w.poll() is False  # inserted, so now the baseline moved
    assert await _count(pool) == 0


async def test_watch_enqueue_db_error_keeps_baseline_and_retries(pool):
    from orchestrator.db.pool import PoolError

    w = _watch(pool, {"1": [10]}, {"1": [10, 11]}, {"1": [10, 11]})
    await w.poll()
    real_write = pool.execute_write
    pool.execute_write = AsyncMock(side_effect=PoolError("db locked"))
    assert await w.poll() is False  # enqueue failed (rowcount 0)
    pool.execute_write = real_write
    assert await w.poll() is True  # baseline kept, so the next poll re-tries
