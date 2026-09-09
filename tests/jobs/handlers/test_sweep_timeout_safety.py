"""Task 6: a timeout or cancellation records an attempt, never cache truth.

Replay of 2026-09-01. When the worker's runtime budget expires it cancels the
sweep handler, and the cancellation lands inside every in-flight per-game
coroutine. A cancelled measurement measured nothing, so it must write no cache
truth — only ``last_measure_attempt_at``, which rotates the interrupted games to
the back so the next run resumes by ordering (Task 5's candidate SQL).

The same holds for any other per-game failure, including the agent's httpx read
timeouts surfaced as ``AgentError``: stamp the attempt, count the error, carry
on.

A ``CircuitBreakerTripped`` is different in kind. The breaker means writing has
STOPPED; a sweep that keeps issuing refused writes for hours is not stopped. So
the sweep aborts: later games short-circuit without validating, and the job
fails with the breaker's message.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest
import structlog.testing as st

import orchestrator.jobs.handlers.sweep as sweep_mod
from orchestrator.core.settings import get_settings
from orchestrator.jobs.handlers.sweep import sweep_handler
from orchestrator.jobs.measurement import CircuitBreakerTripped
from orchestrator.jobs.worker import Deps
from orchestrator.validator.disk_stat import ValidationResult

# No module-level asyncio mark: asyncio_mode = "auto" collects the async tests,
# and the mark would warn on the one synchronous test below.

_app_id_counter = itertools.count(1)

_MEASURED_AT = "2026-01-01 00:00:00"


class _Agent:
    """Truthy AgentClient stand-in — validate_one_game is monkeypatched, so the
    agent only has to be non-None to pass the handler's pre-flight guard."""


def _job() -> dict[str, object]:
    return {"id": 1, "kind": "sweep", "platform": None, "game_id": None}


def _healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(settings, *, agent_client=None):
        return True

    monkeypatch.setattr(sweep_mod, "validator_self_test", _ok)


async def _seed(pool, *, status="up_to_date", status_measured_at=_MEASURED_AT) -> int:
    """Insert one owned game with a known prior measurement, return its id."""
    app_id = f"sweep-timeout-{next(_app_id_counter)}"
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, status_measured_at) "
        "VALUES ('steam', ?, 't', 1, ?, ?)",
        (app_id, status, status_measured_at),
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id=?", (app_id,))
    assert row is not None
    return int(row["id"])


async def _truth(pool, game_id: int) -> dict[str, object]:
    row = await pool.read_one(
        "SELECT status, status_measured_at, last_measure_attempt_at FROM games WHERE id=?",
        (game_id,),
    )
    assert row is not None
    return dict(row)


async def test_cancelled_sweep_writes_no_cache_truth(pool, monkeypatch):
    """The 2026-09-01 replay: a cancelled sweep must corrupt nothing."""
    _healthy(monkeypatch)
    gid = await _seed(pool, status="up_to_date")

    async def slow_validate_one(pool_, deps_, game_id, settings):
        await asyncio.sleep(10)  # still measuring when the budget expires
        raise AssertionError("unreachable — the sweep is cancelled first")

    monkeypatch.setattr(sweep_mod, "validate_one_game", slow_validate_one)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent())), timeout=0.2
        )

    row = await _truth(pool, gid)
    assert row["status"] == "up_to_date", "a cancelled measurement wrote cache truth"
    assert row["status_measured_at"] == _MEASURED_AT, "truth timestamp moved without a measurement"
    assert row["last_measure_attempt_at"] is not None, "the attempt was not recorded"


async def test_per_game_exception_records_attempt_only(pool, monkeypatch):
    """A per-game failure stamps the attempt, keeps the truth, and the sweep goes on."""
    _healthy(monkeypatch)
    boom = await _seed(pool, status="up_to_date")
    fine = await _seed(pool, status="up_to_date")

    validated: list[int] = []

    async def flaky_validate_one(pool_, deps_, game_id, settings):
        if game_id == boom:
            raise RuntimeError("agent read timeout")
        validated.append(game_id)
        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr(sweep_mod, "validate_one_game", flaky_validate_one)
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    failed = await _truth(pool, boom)
    assert failed["status"] == "up_to_date", "a failed measurement wrote cache truth"
    assert failed["status_measured_at"] == _MEASURED_AT
    assert failed["last_measure_attempt_at"] is not None, "the attempt was not recorded"
    assert validated == [fine], "one bad game must not abort the sweep"


async def test_breaker_trip_stops_the_sweep(pool, monkeypatch):
    """A tripped breaker halts the sweep instead of grinding through refused writes."""
    _healthy(monkeypatch)
    batch_size = get_settings().sweep_batch_size
    total = batch_size * 2 + 5
    for _ in range(total):
        await _seed(pool, status="up_to_date")

    seen: list[int] = []

    async def tripping_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        raise CircuitBreakerTripped("42 games lost cache state within 60 minutes; writing halted")

    cap = st.CapturingLogger()
    monkeypatch.setattr(sweep_mod, "validate_one_game", tripping_validate_one)
    monkeypatch.setattr(sweep_mod, "_log", cap)

    with pytest.raises(CircuitBreakerTripped):
        await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert len(seen) < total, "the sweep kept validating after the breaker tripped"
    assert len(seen) <= batch_size, "more than one batch got through the short-circuit"
    aborted = [c for c in cap.calls if c.args and c.args[0] == "sweep.aborted"]
    assert aborted, "sweep.aborted not logged"


def test_timeout_scales_with_chunk_count():
    """Regression pin for #297. The per-game read budget lives in
    ``clients/agent_client.py`` (NOT settings) and scales with
    ``manifests.chunk_count``; ``tests/clients/test_validate_timeout_scaling.py``
    owns the full parametrised contract. ARK ModKit is 359,671 chunks against a
    measured ~433 s need, and a fixed 300 s budget could never pass it."""
    from orchestrator.clients.agent_client import validate_timeout_for

    assert validate_timeout_for(359_671).read > 433
    assert validate_timeout_for(100).read < validate_timeout_for(359_671).read
