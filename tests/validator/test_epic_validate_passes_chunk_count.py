"""The Epic validate caller passes the chunk count it already has.

UAT-14 #297. Scaling the timeout is inert unless the count reaches the client. The
Epic path reads the manifest row before calling — version, cdn_base and raw are all
taken from it — and that same row carries ``chunk_count``, which for game 15035 is
359,671. Not forwarding it would leave the budget at the base and the game would keep
being cut off exactly as before.
"""

from __future__ import annotations

import base64

import pytest

from orchestrator.jobs.worker import Deps
from orchestrator.validator.disk_stat import _validate_epic_game

pytestmark = pytest.mark.asyncio


class _RecordingAgent:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def epic_validate(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "chunks_total": 10,
            "chunks_cached": 10,
            "chunks_missing": 0,
            "outcome": "cached",
            "versions": "21",
            "error": None,
        }


async def _seed(pool, *, chunk_count: int) -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) "
        "VALUES ('epic', 'ARKDevKit', 'ARK ModKit (UE4)', 1, 'up_to_date')"
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id = 'ARKDevKit'")
    game_id = int(row["id"])
    await pool.execute_write(
        "INSERT INTO manifests (game_id, version, raw, chunk_count, total_bytes, cdn_base) "
        "VALUES (?, '21', ?, ?, 261877742712, 'https://cdn.epicgames.com')",
        (game_id, base64.b64decode("aGVsbG8="), chunk_count),
    )
    return game_id


async def test_the_manifests_chunk_count_reaches_the_client(pool) -> None:
    game_id = await _seed(pool, chunk_count=359_671)
    agent = _RecordingAgent()

    await _validate_epic_game(pool, Deps(pool=pool, agent_client=agent), game_id, "ARKDevKit")

    assert agent.calls, "the agent was never called"
    assert agent.calls[0].get("chunk_count") == 359_671, (
        "without the count the budget stays at the 300s base and game 15035 is cut "
        "off exactly as it was before — the scaling would be inert"
    )
