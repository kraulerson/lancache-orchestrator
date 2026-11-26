"""Feature B — the sweep pass can be restarted by a cache eviction (migration 0020).

An uncommanded eviction means cached data may be gone, so every game measured
BEFORE the eviction must be measured again. ``restart_pass`` opens a new pass whose
boundary is the eviction time, marks it ``forced`` (the eviction watch keeps a sweep
queued until it completes) and records the eviction it acted on, so a restart of
the control plane never acts on the same eviction twice.
"""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.sweep import _CANDIDATE_SQL
from orchestrator.jobs.sweep_pass import (
    EVICTION_BOUNDARY_MARGIN_SEC,
    complete_pass,
    read_pass,
    restart_pass,
)

pytestmark = pytest.mark.asyncio


async def _seed(pool, app_id: str, attempted_at: str) -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, last_measure_attempt_at) "
        "VALUES ('steam', ?, ?, 1, ?)",
        (app_id, f"Game {app_id}", attempted_at),
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id=?", (app_id,))
    return int(row["id"])


async def test_fresh_pass_is_not_forced_and_has_no_eviction(pool):
    current = await read_pass(pool)
    assert current.forced is False
    assert current.last_eviction_ts is None


async def test_restart_pass_opens_a_forced_pass_at_the_eviction_time(pool):
    before = await read_pass(pool)
    after = await restart_pass(
        pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z"
    )
    assert after.number == before.number + 1
    # CURRENT_TIMESTAMP format (ledger ruling 2), and the eviction ts plus the
    # burst margin (final-review I1): games measured while the burst continues
    # are still candidates.
    assert after.started_at == "2026-11-23 10:20:00"
    assert after.forced is True
    assert after.last_eviction_ts == "2026-11-23T10:00:00Z"
    assert await read_pass(pool) == after


async def test_games_measured_before_the_eviction_become_candidates_again(pool):
    measured_before = await _seed(pool, "1", "2026-11-23 09:59:59")
    measured_after = await _seed(pool, "2", "2026-11-23 10:20:01")
    state = await restart_pass(
        pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z"
    )
    rows = await pool.read_all(_CANDIDATE_SQL, (state.started_at,))
    ids = [int(r["id"]) for r in rows]
    assert measured_before in ids
    assert measured_after not in ids


async def test_complete_pass_clears_forced(pool):
    state = await restart_pass(
        pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z"
    )
    nxt = await complete_pass(pool, state)
    assert nxt.forced is False
    assert nxt.number == state.number + 1
    assert nxt.last_eviction_ts == "2026-11-23T10:00:00Z", "the ack outlives the pass"


async def test_stale_complete_does_not_clear_a_newer_forced_pass(pool):
    running_sweep_view = await read_pass(pool)
    restarted = await restart_pass(
        pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z"
    )
    after = await complete_pass(pool, running_sweep_view)
    assert after.number == restarted.number
    assert after.forced is True


async def test_game_measured_during_the_eviction_burst_is_still_a_candidate(pool):
    # cache-catcher's 15-min COOLDOWN means the event ts marks the START of a burst
    # that may keep deleting; a game statted 10 min later may have lost chunks since.
    mid_burst = await _seed(pool, "3", "2026-11-23 10:10:00")
    state = await restart_pass(
        pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z"
    )
    rows = await pool.read_all(_CANDIDATE_SQL, (state.started_at,))
    assert mid_burst in [int(r["id"]) for r in rows]


async def _sqlite_now_plus(pool, seconds: int) -> str:
    row = await pool.read_one("SELECT datetime('now', ? || ' seconds') AS t", (str(seconds),))
    return str(row["t"])


async def test_future_eviction_ts_is_clamped_to_now_plus_margin(pool):
    # A NAS clock running ahead must not create a boundary no measurement can pass.
    lo = await _sqlite_now_plus(pool, EVICTION_BOUNDARY_MARGIN_SEC - 5)
    state = await restart_pass(
        pool, started_at="2099-01-01T00:00:00Z", eviction_ts="2099-01-01T00:00:00Z"
    )
    hi = await _sqlite_now_plus(pool, EVICTION_BOUNDARY_MARGIN_SEC + 5)
    assert lo <= state.started_at <= hi
    assert state.last_eviction_ts == "2099-01-01T00:00:00Z", "dedup cursor keeps the raw ts"


async def test_impossible_eviction_ts_falls_back_to_now_plus_margin(pool):
    lo = await _sqlite_now_plus(pool, EVICTION_BOUNDARY_MARGIN_SEC - 5)
    state = await restart_pass(
        pool, started_at="2026-13-01T00:00:00Z", eviction_ts="2026-13-01T00:00:00Z"
    )
    hi = await _sqlite_now_plus(pool, EVICTION_BOUNDARY_MARGIN_SEC + 5)
    assert lo <= state.started_at <= hi
