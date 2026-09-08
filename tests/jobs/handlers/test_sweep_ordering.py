"""Task 5: sweep candidate selection — least-recently-attempted, no status filter.

D1: the old `ORDER BY id` restarted an interrupted sweep at the same low ids
every run. D2: the old `status IN (...)` filter omitted 'not_downloaded',
which made 1357 Steam games invisible to every sweep from 2026-06-18 onward.
"""

from __future__ import annotations

import itertools

import pytest

from orchestrator.jobs.handlers.sweep import _CANDIDATE_SQL, sweep_handler
from orchestrator.jobs.measurement import record_measurement
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio

_app_id_counter = itertools.count(1)


class _Agent:
    """Truthy AgentClient stand-in. validate_one_game is monkeypatched, so the
    agent only needs to be non-None to pass the handler's pre-flight guard."""


def _job():
    return {"id": 1, "kind": "sweep", "platform": None, "game_id": None}


async def _seed_games(pool, *, status="unknown", owned=1, last_measure_attempt_at=None):
    """Insert one game row with an explicit last_measure_attempt_at (including
    NULL) and return its id."""
    app_id = f"sweep-order-{next(_app_id_counter)}"
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, last_measure_attempt_at) "
        "VALUES ('steam', ?, 't', ?, ?, ?)",
        (app_id, owned, status, last_measure_attempt_at),
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id=?", (app_id,))
    assert row is not None
    return int(row["id"])


async def _candidate_ids(pool) -> list[int]:
    """Run the handler's actual gated candidate SQL and return ids in order."""
    rows = await pool.read_all(_CANDIDATE_SQL)
    return [int(r["id"]) for r in rows]


def _healthy(monkeypatch):
    async def _ok(settings, *, agent_client=None):
        return True

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validator_self_test", _ok)


async def _run_sweep(pool, monkeypatch) -> list[int]:
    """Run the gated sweep handler with validate_one_game stubbed to record the
    game ids it was called with, in call order."""
    _healthy(monkeypatch)
    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        from orchestrator.validator.disk_stat import ValidationResult

        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))
    return seen


async def test_not_downloaded_games_are_candidates(pool, monkeypatch):
    """The D2 regression: 1357 Steam games were invisible from 2026-06-18."""
    gid = await _seed_games(pool, status="not_downloaded", owned=1)
    seen = await _run_sweep(pool, monkeypatch)
    assert gid in seen


async def test_oldest_attempt_goes_first(pool):
    new = await _seed_games(
        pool, status="up_to_date", last_measure_attempt_at="2026-09-01 00:00:00"
    )
    old = await _seed_games(
        pool, status="up_to_date", last_measure_attempt_at="2026-06-01 00:00:00"
    )
    never = await _seed_games(pool, status="unknown", last_measure_attempt_at=None)
    assert await _candidate_ids(pool) == [never, old, new]


async def test_failed_attempt_rotates_to_back(pool):
    """Head-of-line blocking: a game that always times out must not pin the queue."""
    stuck = await _seed_games(pool, status="unknown", last_measure_attempt_at=None)
    other = await _seed_games(pool, status="unknown", last_measure_attempt_at="2026-06-01 00:00:00")
    await record_measurement(pool, stuck, "error")
    assert (await _candidate_ids(pool))[0] == other
