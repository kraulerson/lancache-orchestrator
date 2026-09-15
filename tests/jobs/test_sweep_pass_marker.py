"""#311 — the sweep pass marker and the cooperative deadline.

A full validation pass costs ~12.5 h (8.1 TiB at ~650 GiB/h) against a 6 h
``job_max_runtime_sec``. Before this work a sweep re-queried all 3212 candidates
on every run, so it could only emit ``sweep.completed`` by covering the whole
library inside one 6 h window — impossible. 15 of 16 sweeps were recorded
``failed``, the Uptime Kuma sweep monitor could never go green, and nothing
anywhere proved the library had been covered end to end.

These tests pin the three behaviours that fix it:

* candidates are gated on the CURRENT PASS, not on recency, so a game already
  attempted this pass is not attempted again;
* a run that drains its candidate list completes the pass — advancing the
  marker and reporting a clean pass;
* a run that reaches its deadline stops cooperatively, reports honest partial
  progress, and leaves the marker alone, so the job succeeds and the monitor
  stays green without ever claiming a coverage it did not achieve.
"""

from __future__ import annotations

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.handlers.sweep import sweep_handler
from orchestrator.jobs.sweep_pass import complete_pass, read_pass
from orchestrator.jobs.worker import Deps
from orchestrator.validator.disk_stat import ValidationResult

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


class _Agent:
    """Truthy AgentClient stand-in — the handler only checks it is not None."""


def _job(payload: str | None = None):
    return {"id": 1, "kind": "sweep", "platform": None, "game_id": None, "payload": payload}


def _healthy(monkeypatch):
    async def _ok(settings, *, agent_client=None):
        return True

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validator_self_test", _ok)


def _cached(*_a, **_k):
    return ValidationResult(
        chunks_total=1,
        chunks_cached=1,
        chunks_missing=0,
        outcome="cached",
        manifest_version="100",
        error=None,
    )


async def _seed(pool, app_id: str, *, attempted_at: str | None = None) -> int:
    """Insert an owned game, optionally with an explicit attempt stamp."""
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, last_measure_attempt_at) "
        "VALUES ('steam', ?, 't', 1, 'up_to_date', ?)",
        (app_id, attempted_at),
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id=?", (app_id,))
    return int(row["id"])


async def _set_pass(pool, number: int, started_at: str) -> None:
    await pool.execute_write(
        "UPDATE sweep_pass SET pass_number=?, pass_started_at=? WHERE id=1", (number, started_at)
    )


def _settings(**over) -> Settings:
    base = {
        "orchestrator_token": VALID_TOKEN,
        "sweep_batch_size": 1,  # deterministic ordering for the deadline tests
    }
    base.update(over)
    return Settings(**base)


def _use_settings(monkeypatch, settings: Settings) -> None:
    import orchestrator.jobs.handlers.sweep as sweep_mod

    monkeypatch.setattr(sweep_mod, "get_settings", lambda: settings)


def _use_clock(monkeypatch, box: dict[str, float]) -> None:
    """Drive the handler's deadline from a clock the test controls."""
    import orchestrator.jobs.handlers.sweep as sweep_mod

    monkeypatch.setattr(sweep_mod, "monotonic", lambda: box["t"])


# ---------------------------------------------------------------------------
# The marker itself
# ---------------------------------------------------------------------------


async def test_read_pass_returns_the_seeded_pass(pool):
    current = await read_pass(pool)
    assert current.number == 1
    assert current.started_at


async def test_complete_pass_advances_the_number_and_restamps(pool):
    first = await read_pass(pool)
    await _set_pass(pool, first.number, "2026-09-01 00:00:00")
    first = await read_pass(pool)

    second = await complete_pass(pool, first)

    assert second.number == first.number + 1
    assert second.started_at > first.started_at, "a new pass starts now, not when the old one did"
    assert (await read_pass(pool)).number == second.number, "the advance must be persisted"


async def test_complete_pass_is_idempotent_against_a_stale_view(pool):
    """Two callers holding the same pass must not advance it twice.

    The sweep is the only writer today, but a double-advance would skip a whole
    pass number and, worse, restamp pass_started_at a second time — silently
    excusing every game measured in between from the new pass.
    """
    first = await read_pass(pool)
    await complete_pass(pool, first)

    again = await complete_pass(pool, first)  # same stale view

    assert again.number == first.number + 1, "the second advance must be a no-op, not a bump"
    assert (await read_pass(pool)).number == first.number + 1


# ---------------------------------------------------------------------------
# Candidate gating
# ---------------------------------------------------------------------------


async def test_a_game_already_attempted_this_pass_is_not_attempted_again(pool, monkeypatch):
    _healthy(monkeypatch)
    _use_settings(monkeypatch, _settings())
    await _set_pass(pool, 1, "2026-09-10 00:00:00")
    done = await _seed(pool, "done", attempted_at="2026-09-10 06:00:00")  # after pass start
    todo = await _seed(pool, "todo", attempted_at="2026-09-09 06:00:00")  # before pass start

    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        return _cached()

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert seen == [todo], f"only the un-attempted game is a candidate, got {seen}"
    assert done not in seen


async def test_a_never_attempted_game_is_always_a_candidate(pool, monkeypatch):
    _healthy(monkeypatch)
    _use_settings(monkeypatch, _settings())
    await _set_pass(pool, 1, "2026-09-10 00:00:00")
    fresh = await _seed(pool, "fresh", attempted_at=None)

    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        return _cached()

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert seen == [fresh]


async def test_full_mode_ignores_the_pass_marker(pool, monkeypatch):
    """The Game_shelf full-sweep button is a manual override, not part of the
    scheduled convergence cycle: it validates everything, and it must not
    advance the pass — a manual run cannot stand in as proof of pass coverage,
    because it is not gated on the pass at all."""
    _healthy(monkeypatch)
    _use_settings(monkeypatch, _settings())
    await _set_pass(pool, 1, "2026-09-10 00:00:00")
    done = await _seed(pool, "done", attempted_at="2026-09-10 06:00:00")

    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        return _cached()

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    await sweep_handler(_job('{"full": true}'), Deps(pool=pool, agent_client=_Agent()))

    assert seen == [done], "full mode sweeps a game already attempted this pass"
    assert (await read_pass(pool)).number == 1, "a full sweep must not advance the pass"


# ---------------------------------------------------------------------------
# Pass completion
# ---------------------------------------------------------------------------


async def test_draining_the_candidates_completes_the_pass(pool, monkeypatch):
    _healthy(monkeypatch)
    _use_settings(monkeypatch, _settings())
    await _seed(pool, "a", attempted_at=None)
    await _seed(pool, "b", attempted_at=None)

    async def fake_validate_one(pool_, deps_, game_id, settings):
        return _cached()

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    summary = await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert (await read_pass(pool)).number == 2, "a drained candidate list completes the pass"
    assert summary is not None and summary.ok
    assert "complete" in summary.msg.lower(), summary.msg


async def test_a_run_that_finds_nothing_left_completes_the_pass(pool, monkeypatch):
    """The last run of a pass may arrive with every game already attempted. That
    is the pass finishing, not a no-op — it is the only moment at which the
    library is provably covered."""
    _healthy(monkeypatch)
    _use_settings(monkeypatch, _settings())
    await _set_pass(pool, 3, "2026-09-10 00:00:00")
    await _seed(pool, "done", attempted_at="2026-09-10 06:00:00")

    async def fake_validate_one(pool_, deps_, game_id, settings):
        raise AssertionError("nothing should be validated")

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    summary = await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert (await read_pass(pool)).number == 4
    assert summary is not None and summary.ok


async def test_a_game_that_errors_still_leaves_the_candidate_set(pool, monkeypatch):
    """A pass means "every owned game was ATTEMPTED", not "every game measured
    cleanly". An error stamps last_measure_attempt_at, so the game leaves the
    pass; the alternative is one unmeasurable game blocking every future pass
    forever."""
    _healthy(monkeypatch)
    _use_settings(monkeypatch, _settings())
    await _seed(pool, "bad", attempted_at=None)

    async def fake_validate_one(pool_, deps_, game_id, settings):
        raise RuntimeError("agent read timeout")

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    summary = await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert (await read_pass(pool)).number == 2, "an errored game still completes the pass"
    assert summary is not None and summary.ok


# ---------------------------------------------------------------------------
# The cooperative deadline
# ---------------------------------------------------------------------------


async def test_the_deadline_stops_the_run_without_completing_the_pass(pool, monkeypatch):
    """The whole point: a cut-off run must be a SUCCESS that claims nothing.

    The old behaviour was asyncio.wait_for cancelling the handler mid-flight,
    which marked the job failed and pushed Kuma down on a sweep that was working
    perfectly well.
    """
    _healthy(monkeypatch)
    # deadline = 0 + 3600 - 1800 = 1800s; each game costs 1000s of fake clock.
    _use_settings(
        monkeypatch, _settings(job_max_runtime_sec=3600.0, sweep_deadline_margin_sec=1800.0)
    )
    clock = {"t": 0.0}
    _use_clock(monkeypatch, clock)
    for i in range(6):
        await _seed(pool, f"g{i}", attempted_at=None)

    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        clock["t"] += 1000.0
        return _cached()

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    summary = await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert len(seen) == 2, f"games start at t=0 and t=1000; t=2000 is past the deadline: {seen}"
    assert (await read_pass(pool)).number == 1, "a partial run must NOT complete the pass"
    assert summary is not None and summary.ok, "a cut-off but healthy sweep is not a failure"
    assert "partial" in summary.msg.lower(), summary.msg


async def test_the_partial_summary_reports_progress_in_bytes_and_games(pool, monkeypatch):
    """Games/hour misled this project twice: 2455 of 3212 games have no manifest
    and cost nothing, while 3.6 TiB sits in 39 titles. Progress that does not
    name bytes is not progress anyone can act on."""
    _healthy(monkeypatch)
    _use_settings(
        monkeypatch, _settings(job_max_runtime_sec=3600.0, sweep_deadline_margin_sec=1800.0)
    )
    clock = {"t": 0.0}
    _use_clock(monkeypatch, clock)
    for i in range(4):
        gid = await _seed(pool, f"g{i}", attempted_at=None)
        await pool.execute_write("UPDATE games SET size_bytes=? WHERE id=?", (2 * 1024**3, gid))

    async def fake_validate_one(pool_, deps_, game_id, settings):
        clock["t"] += 1000.0
        return _cached()

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    summary = await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert summary is not None
    assert "2/4" in summary.msg or "2 of 4" in summary.msg, summary.msg
    assert "GiB" in summary.msg or "TiB" in summary.msg, (
        f"partial progress must be reported in bytes, not only games: {summary.msg}"
    )


async def test_a_run_with_no_deadline_budget_still_completes(pool, monkeypatch):
    """job_max_runtime_sec = 0 disables the worker's budget entirely (tests, and
    any operator who turns it off). The handler must then have no deadline at
    all rather than computing one in the past and refusing to do any work."""
    _healthy(monkeypatch)
    _use_settings(monkeypatch, _settings(job_max_runtime_sec=0.0))
    clock = {"t": 0.0}
    _use_clock(monkeypatch, clock)
    await _seed(pool, "a", attempted_at=None)

    async def fake_validate_one(pool_, deps_, game_id, settings):
        clock["t"] += 10_000_000.0  # far past any conceivable deadline
        return _cached()

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    summary = await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert (await read_pass(pool)).number == 2
    assert summary is not None and summary.ok


# ---------------------------------------------------------------------------
# The breaker still wins
# ---------------------------------------------------------------------------


async def test_a_tripped_breaker_aborts_without_completing_the_pass(pool, monkeypatch):
    """A tripped breaker means writing has halted — the pass emphatically did
    not complete, and the job must still fail so an operator sees why."""
    from orchestrator.jobs.measurement import CircuitBreakerTripped

    _healthy(monkeypatch)
    _use_settings(monkeypatch, _settings())
    await _seed(pool, "a", attempted_at=None)

    async def fake_validate_one(pool_, deps_, game_id, settings):
        raise CircuitBreakerTripped("26 downward transitions in 24h")

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    with pytest.raises(CircuitBreakerTripped):
        await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    assert (await read_pass(pool)).number == 1, "an aborted sweep must not complete the pass"
