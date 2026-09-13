"""The Steam validate caller passes the size it already knows.

Adversarial review of PR #303. That PR scaled the validate budget by chunk count
and its security audit certified Steam as "backward-compatible: unaffected".
Steam IS unchanged — ``steam_validate`` was called with no count, so it kept the
flat 300s base — and on the post-rebuild hardware (48-54 chunks/sec, measured)
300s covers only ~12,000-16,000 chunks. Every large Steam game therefore still
timed out, wrote NO validation_history row, and could never self-correct: the
exact defect #303 existed to remove, left in place on the platform holding the
biggest games. ARK: Survival Evolved is 369,317 chunks and needs ~7,700s.

"Backward-compatible" was the wrong test. The old behaviour was only safe on
hardware that no longer exists.

Steam self-enumerates agent-side, so unlike Epic there is no manifest row to read
the count from at call time. The orchestrator does, however, hold the previous
run's ``validation_history.chunks_total`` — the same figure the incident analysis
was built on. A first-ever validation has no history and correctly falls back to
the base budget.
"""

from __future__ import annotations

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.worker import Deps
from orchestrator.validator.disk_stat import validate_game

pytestmark = pytest.mark.asyncio

SETTINGS = Settings(orchestrator_token="x" * 32)


class _RecordingAgent:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def steam_validate(self, app_id, **kwargs):
        self.calls.append({"app_id": app_id, **kwargs})
        return {
            "chunks_total": 10,
            "chunks_cached": 10,
            "chunks_missing": 0,
            "outcome": "cached",
            "versions": "1",
            "error": None,
        }


async def _seed_game(pool, app_id: str, title: str) -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) "
        "VALUES ('steam', ?, ?, 1, 'up_to_date')",
        (app_id, title),
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id=?", (app_id,))
    return int(row["id"])


async def _seed_history(pool, game_id: int, chunks_total: int, finished_at: str) -> None:
    await pool.execute_write(
        "INSERT INTO validation_history "
        "(game_id, manifest_version, started_at, finished_at, method, "
        " chunks_total, chunks_cached, chunks_missing, outcome) "
        "VALUES (?, '1', ?, ?, 'disk_stat', ?, ?, 0, 'cached')",
        (game_id, finished_at, finished_at, chunks_total, chunks_total),
    )


async def test_the_last_known_size_reaches_the_client(pool) -> None:
    game_id = await _seed_game(pool, "346110", "ARK: Survival Evolved")
    await _seed_history(pool, game_id, 369_317, "2026-08-30 00:00:00")
    agent = _RecordingAgent()

    await validate_game(pool, Deps(pool=pool, agent_client=agent), game_id, SETTINGS)

    assert agent.calls, "the agent was never called"
    assert agent.calls[0].get("chunk_count") == 369_317, (
        "steam's known size must reach the client, or every large steam game keeps "
        f"the flat base budget and times out; got {agent.calls[0]}"
    )


async def test_the_newest_history_row_wins(pool) -> None:
    """A game that shrank or grew must be budgeted on its most recent measurement."""
    game_id = await _seed_game(pool, "220", "Half-Life 2")
    await _seed_history(pool, game_id, 5_000, "2026-07-01 00:00:00")
    await _seed_history(pool, game_id, 90_000, "2026-08-30 00:00:00")
    agent = _RecordingAgent()

    await validate_game(pool, Deps(pool=pool, agent_client=agent), game_id, SETTINGS)

    assert agent.calls[0].get("chunk_count") == 90_000


async def test_a_never_validated_game_falls_back_to_the_base_budget(pool) -> None:
    """First-ever validation has no history row. That must degrade quietly to the
    base budget rather than raise — a new purchase must still be validatable."""
    game_id = await _seed_game(pool, "440", "Team Fortress 2")
    agent = _RecordingAgent()

    await validate_game(pool, Deps(pool=pool, agent_client=agent), game_id, SETTINGS)

    assert agent.calls, "the agent was never called"
    assert agent.calls[0].get("chunk_count") is None


async def test_a_zero_chunk_history_row_does_not_shrink_the_budget(pool) -> None:
    """An errored prior run can leave chunks_total = 0. Passing that through would
    be read as 'no information' anyway, but it must never produce a budget below
    the base."""
    game_id = await _seed_game(pool, "570", "Dota 2")
    await _seed_history(pool, game_id, 0, "2026-08-30 00:00:00")
    agent = _RecordingAgent()

    await validate_game(pool, Deps(pool=pool, agent_client=agent), game_id, SETTINGS)

    assert agent.calls[0].get("chunk_count") in (None, 0)


async def _seed_error_history(pool, game_id: int, finished_at: str) -> None:
    """An errored run writes a row with chunks_total = 0.

    validate_one_game inserts unconditionally, so this is what the agent being
    briefly unreachable, or the cache being unmounted, leaves behind.
    """
    await pool.execute_write(
        "INSERT INTO validation_history "
        "(game_id, manifest_version, started_at, finished_at, method, "
        " chunks_total, chunks_cached, chunks_missing, outcome, error) "
        "VALUES (?, '1', ?, ?, 'disk_stat', 0, 0, 0, 'error', 'agent unreachable')",
        (game_id, finished_at, finished_at),
    )


async def test_an_error_row_does_not_erase_the_last_known_size(pool) -> None:
    """UAT15-B3 (#308): one transient error must not permanently cripple a big game.

    The budget lookup takes the newest history row regardless of outcome. An error
    row carries chunks_total = 0, which collapses to None and drops ARK from a
    9292s budget to the 300s base. At 300s it times out; the timeout raises
    AgentError out of validate_one_game BEFORE the history insert, so no new row is
    written and the zero row stays newest forever. The game can never be validated
    again and never self-corrects.

    The last row that actually measured something is the only one that carries size
    information. An error measured nothing.
    """
    game_id = await _seed_game(pool, "346111", "ARK: Survival Evolved")
    await _seed_history(pool, game_id, 369_317, "2026-09-11 21:21:44")
    await _seed_error_history(pool, game_id, "2026-09-12 05:57:27")
    agent = _RecordingAgent()

    await validate_game(pool, Deps(pool=pool, agent_client=agent), game_id, SETTINGS)

    assert agent.calls, "the agent was never called"
    assert agent.calls[0].get("chunk_count") == 369_317, (
        "an error row measured nothing and must not erase the last known size; "
        f"got {agent.calls[0].get('chunk_count')!r}, which is the 300s base budget "
        "and guarantees this game times out forever"
    )
