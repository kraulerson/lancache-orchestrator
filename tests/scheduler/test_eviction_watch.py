"""Feature B — EvictionWatch restarts the sweep pass after an eviction and chains sweeps.

The control plane polls the agent for cache-catcher's eviction events (Karl's N1).
A new event restarts the pass at the event time and queues a sweep; while that
forced pass is unfinished, any poll that finds no sweep queued or running queues
the next one. That chaining lives here, not in the sweep: a sweep cannot queue its
own successor because its own row is still `running` and idx_jobs_sweep_inflight
would drop the insert.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.sweep_pass import complete_pass, read_pass, restart_pass
from orchestrator.scheduler.eviction_watch import EvictionWatch
from orchestrator.scheduler.manager import EVICTION_WATCH_JOB_ID, SchedulerManager

pytestmark = pytest.mark.asyncio


def _ev(ts: str, deletes: int = 12) -> dict:
    return {"ts": ts, "deletes": deletes, "window_s": 60, "actor": "other"}


def _watch(pool, *responses):
    agent = AsyncMock()
    agent.eviction_events = AsyncMock(side_effect=list(responses))
    return EvictionWatch(pool, agent), agent


async def _sweeps(pool) -> list[dict]:
    return await pool.read_all("SELECT source, state FROM jobs WHERE kind='sweep' ORDER BY id")


async def test_new_event_restarts_the_pass_and_queues_one_sweep(pool):
    before = await read_pass(pool)
    w, agent = _watch(pool, [_ev("2026-11-23T10:00:00Z")])
    assert await w.poll() == "triggered"
    state = await read_pass(pool)
    assert state.number == before.number + 1
    assert state.started_at == "2026-11-23 10:20:00"
    assert state.forced is True
    assert state.last_eviction_ts == "2026-11-23T10:00:00Z"
    assert await _sweeps(pool) == [{"source": "trigger", "state": "queued"}]
    agent.eviction_events.assert_awaited_once_with(None)


async def test_poll_asks_only_for_events_after_the_last_one_acted_on(pool):
    await restart_pass(pool, started_at="2026-11-20T00:00:00Z", eviction_ts="2026-11-20T00:00:00Z")
    w, agent = _watch(pool, [])
    await w.poll()
    agent.eviction_events.assert_awaited_once_with("2026-11-20T00:00:00Z")


async def test_events_at_or_before_the_ack_do_nothing(pool):
    # The agent filters on since, but a replayed or equal event must still not act.
    state = await restart_pass(
        pool, started_at="2026-11-20T00:00:00Z", eviction_ts="2026-11-20T00:00:00Z"
    )
    await complete_pass(pool, state)
    w, _ = _watch(pool, [_ev("2026-11-20T00:00:00Z"), _ev("2026-11-19T00:00:00Z")])
    before = await read_pass(pool)
    assert await w.poll() == "idle"
    assert await read_pass(pool) == before
    assert await _sweeps(pool) == []


async def test_two_new_events_act_once_at_the_newest(pool):
    w, _ = _watch(pool, [_ev("2026-11-23T10:00:00Z"), _ev("2026-11-23T11:30:00Z")])
    before = await read_pass(pool)
    assert await w.poll() == "triggered"
    state = await read_pass(pool)
    assert state.number == before.number + 1
    assert state.last_eviction_ts == "2026-11-23T11:30:00Z"
    assert state.started_at == "2026-11-23 11:50:00"


async def test_agent_error_changes_nothing(pool):
    w, _ = _watch(pool, RuntimeError("404 Not Found"))
    before = await read_pass(pool)
    assert await w.poll() == "error"
    assert await read_pass(pool) == before
    assert await _sweeps(pool) == []


async def test_forced_pass_with_no_sweep_in_flight_is_chained(pool):
    await restart_pass(pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z")
    w, _ = _watch(pool, [])
    assert await w.poll() == "chained"
    assert await _sweeps(pool) == [{"source": "trigger", "state": "queued"}]


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_forced_pass_with_a_sweep_in_flight_is_not_doubled(pool, state):
    await restart_pass(pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z")
    await pool.execute_write(
        "INSERT INTO jobs (kind, state, source) VALUES ('sweep', ?, 'scheduler')", (state,)
    )
    w, _ = _watch(pool, [])
    assert await w.poll() == "idle"
    assert len(await _sweeps(pool)) == 1


async def test_completed_pass_stops_chaining(pool):
    state = await restart_pass(
        pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z"
    )
    await complete_pass(pool, state)
    w, _ = _watch(pool, [])
    assert await w.poll() == "idle"
    assert await _sweeps(pool) == []


async def _ids(mgr: SchedulerManager) -> list[str]:
    await mgr.start()
    try:
        return mgr.get_registered_job_ids()
    finally:
        await mgr.shutdown()


async def test_manager_registers_eviction_watch(pool):
    mgr = SchedulerManager(
        pool=pool,
        enabled=True,
        library_sync_interval_sec=21600,
        agent_client=AsyncMock(),
        eviction_poll_sec=300,
    )
    assert EVICTION_WATCH_JOB_ID in await _ids(mgr)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"agent_client": None, "eviction_poll_sec": 300},
        {"agent_client": "agent", "eviction_poll_sec": 0},
        {"agent_client": "agent", "eviction_poll_sec": 300, "validation_sweep_enabled": False},
    ],
)
async def test_manager_skips_eviction_watch_unless_all_conditions_hold(pool, kwargs):
    if kwargs.get("agent_client") == "agent":
        kwargs = {**kwargs, "agent_client": AsyncMock()}
    mgr = SchedulerManager(pool=pool, enabled=True, library_sync_interval_sec=21600, **kwargs)
    assert EVICTION_WATCH_JOB_ID not in await _ids(mgr)


async def test_eviction_poll_sec_rejects_sub_minute_values():
    with pytest.raises(ValueError, match="eviction_poll_sec"):
        Settings(orchestrator_token="a" * 32, eviction_poll_sec=30)
    assert Settings(orchestrator_token="a" * 32, eviction_poll_sec=0).eviction_poll_sec == 0
    assert Settings(orchestrator_token="a" * 32).eviction_poll_sec == 300


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_cron_sweep_dedup_promotes_an_inflight_trigger_sweep(pool, state):
    """Final-review I2: a forced chain holds the single sweep slot ~98% of the time.
    A Wednesday cron enqueue that dedups against it must promote the row, or the
    scheduler-only sweep monitor (180) never pushes and reads as a dead scheduler."""
    from orchestrator.scheduler.jobs import enqueue_validation_sweep

    assert await enqueue_validation_sweep(pool, source="trigger") == 1
    await pool.execute_write("UPDATE jobs SET state=? WHERE kind='sweep'", (state,))
    assert await enqueue_validation_sweep(pool) == 0
    assert await _sweeps(pool) == [{"source": "scheduler", "state": state}]


async def test_non_scheduler_sweep_dedup_does_not_promote(pool):
    from orchestrator.scheduler.jobs import enqueue_validation_sweep

    assert await enqueue_validation_sweep(pool, source="trigger") == 1
    assert await enqueue_validation_sweep(pool, source="api") == 0
    assert await _sweeps(pool) == [{"source": "trigger", "state": "queued"}]


async def test_chain_backs_off_after_a_recently_failed_sweep(pool):
    """Ledger ruling 6: a real mass eviction trips the measurement breaker, which
    fails the sweep. Re-chaining every poll would fail a sweep every 5 min; wait out
    the breaker's 60-min window instead."""
    await restart_pass(pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z")
    await pool.execute_write(
        "INSERT INTO jobs (kind, state, source, finished_at, error) "
        "VALUES ('sweep', 'failed', 'trigger', datetime('now', '-10 minutes'), 'breaker')"
    )
    w, _ = _watch(pool, [])
    assert await w.poll() == "idle"
    assert [s["state"] for s in await _sweeps(pool)] == ["failed"]


async def test_chain_resumes_once_the_failed_sweep_is_old_enough(pool):
    await restart_pass(pool, started_at="2026-11-23T10:00:00Z", eviction_ts="2026-11-23T10:00:00Z")
    await pool.execute_write(
        "INSERT INTO jobs (kind, state, source, finished_at, error) "
        "VALUES ('sweep', 'failed', 'trigger', datetime('now', '-61 minutes'), 'breaker')"
    )
    w, _ = _watch(pool, [])
    assert await w.poll() == "chained"
