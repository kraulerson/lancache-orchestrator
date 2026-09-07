"""Circuit breaker on mass cache-state loss (Task 3 of the 2026-09-04 design).

A degraded agent returns *plausible but false* measurements — on 2026-08-31 it
read 65 of 256 cache buckets and reported ~8% cached on every game. Each of
those answers passes every validity check individually; only the shape of the
whole batch gives it away. So ``record_measurement`` counts downward truth
transitions in a rolling window and stops writing when too many arrive.

Upward moves never count (a prefill legitimately flips hundreds of games up),
lifecycle values carry no rank, and an ``error`` outcome is not a measurement at
all.
"""

from __future__ import annotations

import pytest

from orchestrator.clients import heartbeat
from orchestrator.core.settings import get_settings
from orchestrator.jobs.measurement import CircuitBreakerTripped, record_measurement

pytestmark = pytest.mark.asyncio


async def _seed_games(pool, n: int, *, status: str = "up_to_date", prefix: str = "g") -> list[int]:
    """Insert n games with unique app_ids, all at `status`. Returns their ids."""
    ids: list[int] = []
    async with pool.write_transaction() as tx:
        for i in range(n):
            await tx.execute(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', ?, 't', 1, ?)",
                (f"{prefix}{i}", status),
            )
    for i in range(n):
        row = await pool.read_one(
            "SELECT id FROM games WHERE platform='steam' AND app_id=?", (f"{prefix}{i}",)
        )
        ids.append(int(row["id"]))
    return ids


async def _downward_count(pool) -> int:
    row = await pool.read_one("SELECT COUNT(*) AS n FROM measurement_transitions WHERE downward=1")
    return int(row["n"])


async def test_trips_after_25_downward_transitions(pool):
    """The 25th downward transition raises, and writes nothing at all."""
    ids = await _seed_games(pool, 30, status="up_to_date")

    written = 0
    with pytest.raises(CircuitBreakerTripped):
        for gid in ids:
            await record_measurement(pool, gid, "missing")  # up_to_date -> not_downloaded
            written += 1

    assert written == 24  # the 25th call is the one that trips
    row = await pool.read_one("SELECT COUNT(*) AS n FROM games WHERE status='not_downloaded'")
    assert row["n"] == 24
    # A tripped breaker writes nothing: no truth, no transition row.
    assert await _downward_count(pool) == 24
    blocked = await pool.read_one(
        "SELECT status, status_measured_at FROM games WHERE id=?", (ids[24],)
    )
    assert blocked["status"] == "up_to_date"
    assert blocked["status_measured_at"] is None


async def test_upward_transitions_never_trip(pool):
    """A prefill run legitimately flips hundreds of games up. That is not an
    incident, and must never halt writing."""
    ids = await _seed_games(pool, 200, status="unknown")

    for gid in ids:
        await record_measurement(pool, gid, "cached")  # unknown -> up_to_date, rank 0 -> 3

    row = await pool.read_one("SELECT COUNT(*) AS n FROM games WHERE status='up_to_date'")
    assert row["n"] == 200
    assert await _downward_count(pool) == 0
    logged = await pool.read_one("SELECT COUNT(*) AS n FROM measurement_transitions")
    assert logged["n"] == 200  # every truth transition is logged, downward or not


async def test_error_outcomes_never_count(pool):
    """An infrastructure error is not a measurement: no truth write, no
    transition row, and nothing for the breaker to count."""
    ids = await _seed_games(pool, 100, status="up_to_date")

    for gid in ids:
        await record_measurement(pool, gid, "error")

    row = await pool.read_one("SELECT COUNT(*) AS n FROM games WHERE status='up_to_date'")
    assert row["n"] == 100
    logged = await pool.read_one("SELECT COUNT(*) AS n FROM measurement_transitions")
    assert logged["n"] == 0


async def test_lifecycle_states_carry_no_rank(pool):
    """pending_update / downloading / blocked / failed are job lifecycle, not
    cache truth — a move out of one is never 'downward'."""
    for status in ("pending_update", "downloading", "blocked", "failed"):
        ids = await _seed_games(pool, 40, status=status, prefix=f"{status}-")
        for gid in ids:
            await record_measurement(pool, gid, "missing")

    row = await pool.read_one("SELECT COUNT(*) AS n FROM games WHERE status='not_downloaded'")
    assert row["n"] == 160
    assert await _downward_count(pool) == 0


async def test_breaker_holds_open_but_upward_writes_still_land(pool):
    """Once tripped the breaker keeps refusing downward writes — it does not
    reset itself by having raised. Upward measurements are unaffected."""
    ids = await _seed_games(pool, 30, status="up_to_date")
    with pytest.raises(CircuitBreakerTripped):
        for gid in ids:
            await record_measurement(pool, gid, "missing")

    # Still open for the next downward call.
    with pytest.raises(CircuitBreakerTripped):
        await record_measurement(pool, ids[25], "missing")

    upward = (await _seed_games(pool, 1, status="unknown", prefix="up-"))[0]
    await record_measurement(pool, upward, "cached")

    row = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (upward,))
    assert row["status"] == "up_to_date"
    assert row["status_measured_at"] is not None


async def test_window_slides_so_old_transitions_stop_counting(pool):
    """The count is a rolling window, not a lifetime total: transitions older
    than the window drop out and the breaker closes again."""
    ids = await _seed_games(pool, 30, status="up_to_date")
    for gid in ids[:24]:
        await record_measurement(pool, gid, "missing")
    assert await _downward_count(pool) == 24  # one short of the threshold

    await pool.execute_write(
        "UPDATE measurement_transitions SET occurred_at = datetime('now', '-61 minutes')"
    )

    await record_measurement(pool, ids[24], "missing")  # would have tripped a minute ago
    row = await pool.read_one("SELECT status FROM games WHERE id=?", (ids[24],))
    assert row["status"] == "not_downloaded"


async def test_trip_pushes_down_to_kuma(pool, monkeypatch):
    """A trip is silent unless someone is told. Delivery is Kuma's job — we
    reuse the existing heartbeat emitter rather than speaking HTTP here."""
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc123")
    get_settings.cache_clear()

    calls: list[dict[str, object]] = []

    async def _record(url, *, status, msg="", transport=None):
        calls.append({"url": url, "status": status, "msg": msg})

    monkeypatch.setattr(heartbeat, "push", _record)

    ids = await _seed_games(pool, 30, status="up_to_date")
    with pytest.raises(CircuitBreakerTripped):
        for gid in ids:
            await record_measurement(pool, gid, "missing")

    assert len(calls) == 1
    assert calls[0]["url"] == "http://kuma.test/api/push/abc123"
    assert calls[0]["status"] == "down"
    assert "25" in str(calls[0]["msg"])


async def test_no_push_when_no_url_configured(pool, monkeypatch):
    """Unset is the documented way to disable a Kuma monitor."""
    calls: list[str] = []

    async def _record(url, *, status, msg="", transport=None):
        calls.append(status)

    monkeypatch.setattr(heartbeat, "push", _record)

    ids = await _seed_games(pool, 30, status="up_to_date")
    with pytest.raises(CircuitBreakerTripped):
        for gid in ids:
            await record_measurement(pool, gid, "missing")

    assert calls == []


async def test_failed_push_does_not_suppress_the_trip(pool, monkeypatch):
    """A dead monitor must never swallow the exception that halts writing."""
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc123")
    get_settings.cache_clear()

    async def _boom(url, *, status, msg="", transport=None):
        raise RuntimeError("kuma is on fire")

    monkeypatch.setattr(heartbeat, "push", _boom)

    ids = await _seed_games(pool, 30, status="up_to_date")
    with pytest.raises(CircuitBreakerTripped):
        for gid in ids:
            await record_measurement(pool, gid, "missing")


async def test_threshold_and_window_are_configurable(pool, monkeypatch):
    """Operators tune both without a code change."""
    monkeypatch.setenv("ORCH_MEASUREMENT_BREAKER_THRESHOLD", "3")
    get_settings.cache_clear()

    ids = await _seed_games(pool, 10, status="up_to_date")
    written = 0
    with pytest.raises(CircuitBreakerTripped):
        for gid in ids:
            await record_measurement(pool, gid, "missing")
            written += 1

    assert written == 2
    assert get_settings().measurement_breaker_window_minutes == 60


async def test_transition_rows_record_the_move(pool):
    """The log is the breaker's memory across restarts, so it has to be
    readable: prior, new status, and the downward flag."""
    game_id = (await _seed_games(pool, 1, status="up_to_date"))[0]

    await record_measurement(pool, game_id, "partial")

    row = await pool.read_one(
        "SELECT game_id, prior, new_status, downward, occurred_at "
        "FROM measurement_transitions WHERE game_id=?",
        (game_id,),
    )
    assert row["game_id"] == game_id
    assert row["prior"] == "up_to_date"
    assert row["new_status"] == "validation_failed"
    assert row["downward"] == 1
    assert row["occurred_at"] is not None


async def test_breaker_counts_inside_an_open_transaction(pool):
    """The tx path must enlist reads as well as writes, or the breaker counts
    against a snapshot that predates the caller's own transitions."""
    ids = await _seed_games(pool, 30, status="up_to_date")

    with pytest.raises(CircuitBreakerTripped):
        async with pool.write_transaction() as tx:
            for gid in ids:
                await record_measurement(pool, gid, "missing", tx=tx)

    # The trip aborted the caller's transaction, so nothing survived it.
    row = await pool.read_one("SELECT COUNT(*) AS n FROM games WHERE status='not_downloaded'")
    assert row["n"] == 0
    assert await _downward_count(pool) == 0
