"""#388 — a newly purchased Epic game must reach scheduled prefill.

The deadlock this guards: an Epic manifest is stored only by a prefill; a
game without one validated as an error, which writes no cache truth, so it
stayed 'unknown'; and scheduled prefill takes only games measured absent. On
2026-11-12 twelve owned Epic games had sat there through 421 validations.
"""

from __future__ import annotations

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.handlers.validate import validate_one_game
from orchestrator.jobs.worker import Deps
from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

pytestmark = pytest.mark.asyncio


async def _epic_game(pool, app_id: str, *, prefilled: bool) -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, last_prefilled_at) "
        "VALUES ('epic', ?, ?, 1, 'unknown', "
        + ("CURRENT_TIMESTAMP" if prefilled else "NULL")
        + ")",
        (app_id, app_id),
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id = ?", (app_id,))
    return int(row["id"])


async def test_a_never_prefilled_epic_game_is_measured_and_queued_for_prefill(pool):
    game_id = await _epic_game(pool, "newpurchase", prefilled=False)

    await validate_one_game(
        pool, Deps(pool=pool, agent_client=object()), game_id, Settings(orchestrator_token="a" * 32)
    )

    row = await pool.read_one(
        "SELECT status, status_measured_at FROM games WHERE id = ?", (game_id,)
    )
    assert row["status"] == "not_downloaded"
    assert row["status_measured_at"] is not None, "it must count as a real measurement"
    assert await enqueue_scheduled_prefill(pool) == 1


async def test_a_block_listed_epic_game_keeps_its_status(pool):
    """Review finding 2: the 15 games migration 0018 retired to 'blocked' have no
    manifest and were never prefilled. Measuring them 'missing' would undo 0018,
    stamp a measurement nobody took, and count them twice on Game_shelf's
    'Not cached' tile — for nothing, because prefill skips block-listed games."""
    game_id = await _epic_game(pool, "blocked404", prefilled=False)
    await pool.execute_write("UPDATE games SET status = 'blocked' WHERE id = ?", (game_id,))
    await pool.execute_write(
        "INSERT INTO block_list (platform, app_id, reason) "
        "VALUES ('epic', 'blocked404', 'HTTP 404')"
    )

    await validate_one_game(
        pool, Deps(pool=pool, agent_client=object()), game_id, Settings(orchestrator_token="a" * 32)
    )

    row = await pool.read_one(
        "SELECT status, status_measured_at FROM games WHERE id = ?", (game_id,)
    )
    assert row["status"] == "blocked"
    assert row["status_measured_at"] is None


async def test_a_prefill_excluded_epic_game_keeps_its_status(pool):
    """An excluded game is never prefilled, so 'not downloaded' would be a
    permanent label nothing could correct — false if a LAN client installed it."""
    game_id = await _epic_game(pool, "excluded", prefilled=False)
    await pool.execute_write(
        "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
        "VALUES ('epic', 'excluded', 'exclude', 'operator')"
    )

    await validate_one_game(
        pool, Deps(pool=pool, agent_client=object()), game_id, Settings(orchestrator_token="a" * 32)
    )

    row = await pool.read_one(
        "SELECT status, status_measured_at FROM games WHERE id = ?", (game_id,)
    )
    assert row["status"] == "unknown"
    assert row["status_measured_at"] is None


async def test_an_allow_override_does_not_count_as_an_exclusion(pool):
    """An 'allow' row forces prefill ON, so it must not block the measurement."""
    game_id = await _epic_game(pool, "allowed", prefilled=False)
    await pool.execute_write(
        "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
        "VALUES ('epic', 'allowed', 'allow', 'operator')"
    )

    await validate_one_game(
        pool, Deps(pool=pool, agent_client=object()), game_id, Settings(orchestrator_token="a" * 32)
    )

    row = await pool.read_one("SELECT status FROM games WHERE id = ?", (game_id,))
    assert row["status"] == "not_downloaded"


async def test_a_prefilled_epic_game_with_no_manifest_is_not_queued(pool):
    game_id = await _epic_game(pool, "brokenmanifest", prefilled=True)

    await validate_one_game(
        pool, Deps(pool=pool, agent_client=object()), game_id, Settings(orchestrator_token="a" * 32)
    )

    row = await pool.read_one(
        "SELECT status, status_measured_at FROM games WHERE id = ?", (game_id,)
    )
    assert row["status"] == "unknown"
    assert row["status_measured_at"] is None
    assert await enqueue_scheduled_prefill(pool) == 0
