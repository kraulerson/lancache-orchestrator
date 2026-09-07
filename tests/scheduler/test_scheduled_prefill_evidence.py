"""Task 7: evidence-based Epic download policy.

Design 2026-09-04. `enqueue_scheduled_prefill()` previously selected Epic games
on `status <> 'up_to_date'`, meaning "not proven cached" == "download it". On
2026-09-01 an interrupted prefill batch corrupted 1769 games' status; replayed
against the old predicate that would have queued 655 downloads for titles
already on disk. A download now requires a REAL measurement (`status_measured_at`
set) that found the game absent or incomplete -- `status` alone is never enough.
"""

from __future__ import annotations

import pytest

from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

pytestmark = pytest.mark.asyncio


async def _seed_game(
    pool,
    app_id,
    *,
    owned=1,
    status="unknown",
    platform="epic",
    status_measured_at=None,
):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, status_measured_at) "
        "VALUES (?, ?, 'G', ?, ?, ?)",
        (platform, app_id, owned, status, status_measured_at),
    )


async def test_unknown_games_are_never_queued(pool):
    """The 655-download bug: 'not proven cached' must not mean 'download it'."""
    await _seed_game(pool, "1", status="unknown", status_measured_at=None)
    assert await enqueue_scheduled_prefill(pool) == 0


async def test_measured_missing_game_is_queued(pool):
    await _seed_game(pool, "1", status="not_downloaded", status_measured_at="2026-09-04 12:00:00")
    assert await enqueue_scheduled_prefill(pool) == 1


async def test_unmeasured_missing_game_is_not_queued(pool):
    """Status says missing but nothing ever measured it -- no evidence, no download."""
    await _seed_game(pool, "1", status="not_downloaded", status_measured_at=None)
    assert await enqueue_scheduled_prefill(pool) == 0


async def test_measured_partial_game_is_queued(pool):
    """A measured partial (validation_failed) is queued -- the partial case."""
    await _seed_game(
        pool, "1", status="validation_failed", status_measured_at="2026-09-04 12:00:00"
    )
    assert await enqueue_scheduled_prefill(pool) == 1


async def test_legacy_failed_status_is_not_queued_even_if_measured(pool):
    """'failed' is a job outcome (a prefill attempt died), not evidence of
    absence. Nothing writes this value any more, but a legacy row with
    status_measured_at set must still not trigger a download."""
    await _seed_game(pool, "1", status="failed", status_measured_at="2026-09-04 12:00:00")
    assert await enqueue_scheduled_prefill(pool) == 0
