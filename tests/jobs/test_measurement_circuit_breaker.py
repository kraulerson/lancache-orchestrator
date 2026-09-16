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

import time

import pytest
from structlog.testing import capture_logs

from orchestrator.clients import heartbeat
from orchestrator.core.settings import get_settings
from orchestrator.jobs import measurement
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


async def _breaker_count(pool) -> int:
    """What the breaker itself counts: observed losses only, never commanded ones."""
    row = await pool.read_one(
        "SELECT COUNT(*) AS n FROM measurement_transitions WHERE downward=1 AND commanded=0"
    )
    return int(row["n"])


async def test_trips_on_the_25th_downward_transition(pool):
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
        return True

    monkeypatch.setattr(heartbeat, "push", _record)

    ids = await _seed_games(pool, 30, status="up_to_date")
    with pytest.raises(CircuitBreakerTripped):
        for gid in ids:
            await record_measurement(pool, gid, "missing")

    assert len(calls) == 1
    assert calls[0]["url"] == "http://kuma.test/api/push/abc123"
    assert calls[0]["status"] == "down"
    assert "25" in str(calls[0]["msg"])


async def _refuse_everything(pool, ids) -> int:
    """Measure every game 'missing', swallowing the trips. Returns the refusal
    count. This is what production actually does: the sweep's `except Exception`
    keeps going, so the breaker sees hundreds of refused writes in a row, not one."""
    refused = 0
    for gid in ids:
        try:
            await record_measurement(pool, gid, "missing")
        except CircuitBreakerTripped:
            refused += 1
    return refused


async def test_repeated_refusals_notify_once(pool, monkeypatch):
    """One trip, one push. The count is frozen once the breaker is open (a
    refused write records no transition), so every later downward call
    recomputes the same number — that must not become one Kuma GET per game."""
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc123")
    get_settings.cache_clear()

    calls: list[str] = []

    async def _record(url, *, status, msg="", transport=None):
        calls.append(status)
        return True

    monkeypatch.setattr(heartbeat, "push", _record)

    ids = await _seed_games(pool, 30, status="up_to_date")
    refused = await _refuse_everything(pool, ids)

    assert refused == 6  # 24 written, 6 refused — every one of them raised
    assert calls == ["down"]  # ...and exactly one of them notified


async def test_repeated_refusals_log_error_once_then_info(pool, monkeypatch):
    """The ERROR line is the operator's page; it fires once per open trip.
    Later refusals stay visible at INFO so the scale is still recoverable from
    the log, without a thousand ERROR lines."""
    ids = await _seed_games(pool, 30, status="up_to_date")

    with capture_logs() as logs:
        await _refuse_everything(pool, ids)

    tripped = [e for e in logs if e["event"] == "measurement.breaker_tripped"]
    assert len(tripped) == 1
    assert tripped[0]["log_level"] == "error"
    assert tripped[0]["downward_in_window"] == 25

    refused = [e for e in logs if e["event"] == "measurement.breaker_refused"]
    assert len(refused) == 5
    assert {e["log_level"] for e in refused} == {"info"}
    assert refused[0]["game_id"] == ids[25]
    assert refused[0]["prior"] == "up_to_date"
    assert refused[0]["new_status"] == "not_downloaded"
    assert refused[0]["downward_in_window"] == 25


async def test_notification_repeats_once_the_dedupe_window_elapses(pool, monkeypatch):
    """Suppression is time-boxed, not permanent: a breaker still open an hour
    later is still news, and Kuma needs a fresh DOWN to stay red."""
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc123")
    get_settings.cache_clear()

    calls: list[str] = []

    async def _record(url, *, status, msg="", transport=None):
        calls.append(status)
        return True

    monkeypatch.setattr(heartbeat, "push", _record)

    ids = await _seed_games(pool, 30, status="up_to_date")
    await _refuse_everything(pool, ids)
    assert len(calls) == 1

    # Push the next-allowed deadline into the past: the 60-minute window that
    # one delivered notice bought has elapsed.
    monkeypatch.setattr(measurement, "_next_breaker_notice_at", time.monotonic() - 1)

    with pytest.raises(CircuitBreakerTripped):
        await record_measurement(pool, ids[25], "missing")

    assert calls == ["down", "down"]


async def test_no_push_when_no_url_configured(pool, monkeypatch):
    """Unset is the documented way to disable a Kuma monitor."""
    calls: list[str] = []

    async def _record(url, *, status, msg="", transport=None):
        calls.append(status)
        return True

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
    monkeypatch.setenv("ORCH_MEASUREMENT_BREAKER_WINDOW_MINUTES", "5")
    get_settings.cache_clear()
    assert get_settings().measurement_breaker_window_minutes == 5  # the env var binds

    ids = await _seed_games(pool, 10, status="up_to_date")
    written = 0
    with pytest.raises(CircuitBreakerTripped) as excinfo:
        for gid in ids:
            await record_measurement(pool, gid, "missing")
            written += 1

    assert written == 2
    assert "within 5 minutes" in str(excinfo.value)  # the configured window, not the default


async def test_transitions_outside_a_shortened_window_stop_counting(pool, monkeypatch):
    """The window setting really drives the count, not just the message."""
    monkeypatch.setenv("ORCH_MEASUREMENT_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("ORCH_MEASUREMENT_BREAKER_WINDOW_MINUTES", "5")
    get_settings.cache_clear()

    ids = await _seed_games(pool, 10, status="up_to_date")
    for gid in ids[:2]:
        await record_measurement(pool, gid, "missing")
    # 6 minutes old: inside the 60-minute default, outside the configured 5.
    await pool.execute_write(
        "UPDATE measurement_transitions SET occurred_at = datetime('now', '-6 minutes')"
    )

    await record_measurement(pool, ids[2], "missing")
    row = await pool.read_one("SELECT status FROM games WHERE id=?", (ids[2],))
    assert row["status"] == "not_downloaded"


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


async def test_a_commanded_change_is_never_refused(pool):
    """Security audit SEV-2: the breaker vetoes observations, never commands.

    An operator purge has already deleted the files by the time the record is
    written. Refusing it does not preserve truth — it destroys the only record of
    a change that really happened, and the sweep that is supposed to correct the
    divergence is halted by the same breaker.
    """
    ids = await _seed_games(pool, 30, status="up_to_date")
    for gid in ids[:24]:
        await record_measurement(pool, gid, "missing")
    assert await _downward_count(pool) == 24  # one short of the threshold

    await record_measurement(pool, ids[24], "missing", commanded=True)

    row = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (ids[24],))
    assert row["status"] == "not_downloaded"
    assert row["status_measured_at"] is not None


async def test_a_commanded_change_is_logged_but_not_counted(pool):
    """Known cache truth is not evidence of unexplained mass loss.

    Counting commands would let a legitimate batch of purges arm the alarm
    against the sweep that follows them.

    Until #310 that immunity was bought by writing no transition row at all,
    which left the largest deliberate cache change the system can make absent
    from the one immutable record of cache truth. The row is written now and
    marked ``commanded``; the breaker's window count reads only unmarked rows, so
    the immunity is unchanged and the history is no longer a blank.
    """
    ids = await _seed_games(pool, 30, status="up_to_date")
    for gid in ids[:24]:
        await record_measurement(pool, gid, "missing")

    await record_measurement(pool, ids[24], "missing", commanded=True)

    assert await _breaker_count(pool) == 24, "the command must not arm the alarm"
    rows = await pool.read_all(
        "SELECT downward, commanded FROM measurement_transitions WHERE game_id=?", (ids[24],)
    )
    assert len(rows) == 1, "the command is cache truth and belongs in the log (#310)"
    assert rows[0]["downward"] == 1, "losing a cached copy is a loss however it was caused"
    assert rows[0]["commanded"] == 1


async def test_a_commanded_change_logs_itself(pool):
    """The log line names the operator action behind the marked transition row."""
    game_id = (await _seed_games(pool, 1, status="up_to_date"))[0]

    with capture_logs() as logs:
        await record_measurement(pool, game_id, "missing", commanded=True)

    commanded = [e for e in logs if e["event"] == "measurement.commanded"]
    assert len(commanded) == 1
    assert commanded[0]["log_level"] == "info"
    assert commanded[0]["game_id"] == game_id
    assert commanded[0]["prior"] == "up_to_date"
    assert commanded[0]["new_status"] == "not_downloaded"


async def test_commanded_does_not_change_the_error_path(pool):
    """'error' is not a measurement whatever commanded it — still attempt-only."""
    game_id = (await _seed_games(pool, 1, status="up_to_date"))[0]

    await record_measurement(pool, game_id, "error", commanded=True)

    row = await pool.read_one(
        "SELECT status, status_measured_at, last_measure_attempt_at FROM games WHERE id=?",
        (game_id,),
    )
    assert row["status"] == "up_to_date"
    assert row["status_measured_at"] is None
    assert row["last_measure_attempt_at"] is not None


# ---------------------------------------------------------------------------
# #313 — a push that never arrived must not burn the dedupe window
# ---------------------------------------------------------------------------


async def test_an_undelivered_push_does_not_silence_the_incident(pool, monkeypatch):
    """The defect: the stamp was set BEFORE the push was attempted, and
    heartbeat.push swallowed its own failures, so one unreachable moment
    silenced a library-wide incident for the whole 60-minute window.

    This matters more than it sounds: the sweep aborts on the FIRST trip, so a
    sweep makes exactly one push attempt — and a NAS or network fault is
    precisely correlated with the mass eviction that trips the breaker. The two
    failure modes are not independent.
    """
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc123")
    get_settings.cache_clear()

    delivered: list[bool] = [False, True]
    attempts: list[str] = []

    async def _flaky(url, *, status, msg="", transport=None):
        attempts.append(status)
        return delivered.pop(0) if delivered else True

    monkeypatch.setattr(heartbeat, "push", _flaky)
    monkeypatch.setattr(measurement, "_BREAKER_RETRY_SEC", 0.0)

    ids = await _seed_games(pool, 30, status="up_to_date")
    await _refuse_everything(pool, ids)

    assert len(attempts) >= 2, (
        f"the failed push must be retried, not swallowed for the window: {attempts}"
    )


async def test_a_delivered_push_still_suppresses_for_the_window(pool, monkeypatch):
    """The stamp exists for a reason: a refused write records no transition, so
    the count stays frozen and every later downward measurement recomputes the
    same trip. Without suppression an incident-scale sweep emits one 10-second
    Kuma GET per remaining game. Fixing #313 must not reopen that."""
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc123")
    get_settings.cache_clear()

    attempts: list[str] = []

    async def _ok(url, *, status, msg="", transport=None):
        attempts.append(status)
        return True

    monkeypatch.setattr(heartbeat, "push", _ok)

    ids = await _seed_games(pool, 30, status="up_to_date")
    await _refuse_everything(pool, ids)

    assert attempts == ["down"], f"one delivered push should suppress the rest: {attempts}"


async def test_a_failed_push_backs_off_instead_of_retrying_every_game(pool, monkeypatch):
    """The honest middle: retry a lost notification, but not once per game.

    With the retry backoff intact, a Kuma that is down for the whole incident
    must not produce one 10-second-timeout GET per refused write — that is the
    original defect the stamp was introduced to fix.
    """
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc123")
    get_settings.cache_clear()

    attempts: list[str] = []

    async def _always_fails(url, *, status, msg="", transport=None):
        attempts.append(status)
        return False

    monkeypatch.setattr(heartbeat, "push", _always_fails)

    ids = await _seed_games(pool, 40, status="up_to_date")
    await _refuse_everything(pool, ids)

    assert len(attempts) == 1, (
        f"a dead monitor must not be retried once per game inside the backoff: {attempts}"
    )
