"""#333 — three tests that prove the property they are named for.

UAT session 16's automated-suite agent found three places where a test passed
without demonstrating anything:

1. the `_BREAKER_RETRY_SEC` backoff was "proved" by a tight sequential loop whose
   real elapsed time is milliseconds — it would pass identically if the constant
   were an hour, or absent;
2. the `_breaker_notice_due()` / `_stamp_breaker_notice()` split is a
   check-then-act across an `await`, and every dedupe test drives
   `record_measurement` sequentially, so the concurrent case production actually
   runs at was never exercised;
3. the sweep's deadline-before-validate ordering was only ever asserted with
   `sweep_batch_size: 1`, forced in every test "for deterministic ordering",
   while production defaults to 2.

These use a controllable clock and real concurrency so the properties are
actually demonstrated.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from orchestrator.clients import heartbeat
from orchestrator.core.settings import Settings, get_settings
from orchestrator.jobs import measurement
from orchestrator.jobs.handlers.sweep import sweep_handler
from orchestrator.jobs.measurement import CircuitBreakerTripped, record_measurement
from orchestrator.jobs.worker import Deps
from orchestrator.validator.disk_stat import ValidationResult
from tests.jobs.test_measurement_circuit_breaker import _seed_games

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


class _Clock:
    """A monotonic clock the test drives. `measurement` reads time.monotonic()."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ---------------------------------------------------------------------------
# 1 — the backoff actually bounds the retry rate
# ---------------------------------------------------------------------------


async def test_a_failed_push_is_not_retried_before_the_backoff_elapses(pool, monkeypatch):
    """Would pass whatever _BREAKER_RETRY_SEC held, until the clock is driven."""
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc")
    get_settings.cache_clear()
    clock = _Clock()
    monkeypatch.setattr(measurement.time, "monotonic", clock.monotonic)

    attempts: list[float] = []

    async def _always_fails(url, *, status, msg="", transport=None):
        attempts.append(clock.t)
        return False

    monkeypatch.setattr(heartbeat, "push", _always_fails)

    # Each trip needs a game still at up_to_date: re-measuring an already
    # not_downloaded game is not a DOWNWARD transition and would not trip.
    ids = await _seed_games(pool, 40, status="up_to_date")
    for gid in ids[:25]:
        with contextlib.suppress(CircuitBreakerTripped):
            await record_measurement(pool, gid, "missing")
    assert len(attempts) == 1, "the first trip notifies"

    # 59 seconds after a FAILED push: still inside the 60s backoff.
    clock.advance(59)
    with pytest.raises(CircuitBreakerTripped):
        await record_measurement(pool, ids[25], "missing")
    assert len(attempts) == 1, "retried before the backoff elapsed"

    # 61 seconds: the backoff is over, so the lost notification is retried.
    clock.advance(2)
    with pytest.raises(CircuitBreakerTripped):
        await record_measurement(pool, ids[26], "missing")
    assert len(attempts) == 2, "a lost notification must be retried once the backoff elapses"


async def test_a_delivered_push_suppresses_for_the_whole_window_not_the_backoff(pool, monkeypatch):
    """The two delays must be different. If a delivered push only bought the 60s
    backoff, an incident-scale sweep would still emit a push a minute."""
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc")
    get_settings.cache_clear()
    clock = _Clock()
    monkeypatch.setattr(measurement.time, "monotonic", clock.monotonic)

    attempts: list[float] = []

    async def _delivers(url, *, status, msg="", transport=None):
        attempts.append(clock.t)
        return True

    monkeypatch.setattr(heartbeat, "push", _delivers)

    ids = await _seed_games(pool, 40, status="up_to_date")
    for gid in ids[:25]:
        with contextlib.suppress(CircuitBreakerTripped):
            await record_measurement(pool, gid, "missing")
    assert len(attempts) == 1

    clock.advance(61)  # past the retry backoff, far short of the 60-minute window
    with pytest.raises(CircuitBreakerTripped):
        await record_measurement(pool, ids[25], "missing")
    assert len(attempts) == 1, "a DELIVERED push must hold for the window, not the backoff"


# ---------------------------------------------------------------------------
# 2 — the check/stamp split under the concurrency production actually runs
# ---------------------------------------------------------------------------


async def test_concurrent_trips_notify_once(pool, monkeypatch):
    """`_breaker_notice_due()` checks, then the push is awaited, then the clock is
    stamped. Two coroutines can pass the check before either stamps — and the
    sweep validates games concurrently (sweep_batch_size defaults to 2), so this
    is the shape production runs, not a contrived one."""
    monkeypatch.setenv("ORCH_KUMA_PUSH_MEASUREMENT_BREAKER", "http://kuma.test/api/push/abc")
    get_settings.cache_clear()

    attempts: list[str] = []

    async def _slow_push(url, *, status, msg="", transport=None):
        attempts.append(status)
        await asyncio.sleep(0.05)  # the real await the race lives across
        return True

    monkeypatch.setattr(heartbeat, "push", _slow_push)

    ids = await _seed_games(pool, 30, status="up_to_date")
    # Arm the breaker: write enough downward transitions to sit at the threshold.
    for gid in ids[:24]:
        await record_measurement(pool, gid, "missing")

    async def _trip(gid: int) -> None:
        with contextlib.suppress(CircuitBreakerTripped):
            await record_measurement(pool, gid, "missing")

    await asyncio.gather(*(_trip(g) for g in ids[24:30]))

    assert len(attempts) == 1, (
        f"concurrent trips must notify once, got {len(attempts)} pushes — "
        "the dedupe window is taken after the await, so the check is not atomic"
    )


# ---------------------------------------------------------------------------
# 3 — the sweep deadline holds at the concurrency production uses
# ---------------------------------------------------------------------------


async def test_no_game_starts_validating_after_the_deadline_at_batch_size_two(pool, monkeypatch):
    """Every existing deadline test forces sweep_batch_size=1. Production runs 2,
    where several coroutines are inside the semaphore at once."""
    import orchestrator.jobs.handlers.sweep as sweep_mod

    async def _healthy(settings, *, agent_client=None):
        return True

    monkeypatch.setattr(sweep_mod, "validator_self_test", _healthy)
    settings = Settings(
        orchestrator_token=VALID_TOKEN,
        sweep_batch_size=2,
        job_max_runtime_sec=3600.0,
        sweep_deadline_margin_sec=1800.0,
    )
    monkeypatch.setattr(sweep_mod, "get_settings", lambda: settings)

    clock = _Clock()
    clock.t = 0.0
    monkeypatch.setattr(sweep_mod, "monotonic", clock.monotonic)
    deadline = 1800.0  # 0 + 3600 - 1800

    started_at: list[float] = []

    async def fake_validate_one(pool_, deps_, game_id, settings_):
        started_at.append(clock.t)
        await asyncio.sleep(0)  # yield, so both semaphore slots interleave
        clock.advance(500.0)
        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr(sweep_mod, "validate_one_game", fake_validate_one)

    for i in range(10):
        await pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned, status) "
            "VALUES ('steam', ?, 't', 1, 'up_to_date')",
            (f"dl{i}",),
        )

    summary = await sweep_handler(
        {"id": 1, "kind": "sweep", "payload": None}, Deps(pool=pool, agent_client=object())
    )

    late = [t for t in started_at if t >= deadline]
    assert late == [], (
        f"games started validating at {late} — at or after the deadline {deadline}. "
        "The check must happen immediately before each validate, at any batch size."
    )
    assert summary is not None and "partial" in summary.msg.lower()
