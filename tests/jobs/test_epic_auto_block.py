"""#388 review finding 1 — stop the endless retry of an Epic game Epic won't serve.

Once a never-prefilled game is measured 'not_downloaded', scheduled prefill
takes it every slot. If Epic's manifest API or manifest download answers a
4xx, it fails every time, forever (The Ouroboros King: 34 failures in 9 days
until block-listed by hand). Karl, 2026-11-12: block it automatically after
10 consecutive such failures. 401 (auth refresh) and 429 (rate limit) are
transient and never count.
"""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.prefill import prefill_handler
from orchestrator.jobs.worker import Deps
from orchestrator.platform.epic.manifest import EpicManifestError

pytestmark = pytest.mark.asyncio

_API_404 = "EpicManifestError: epic manifest API failed: HTTP 404"
_DL_403 = "EpicManifestError: epic manifest download failed: HTTP 403"


class _FailingEpicClient:
    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.message = message
        self.status_code = status_code

    async def fetch_manifest(self, item):
        raise EpicManifestError(self.message, status_code=self.status_code)


async def _game(pool) -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) "
        "VALUES ('epic', 'ouroboros', 'The Ouroboros King', 1, 'not_downloaded')"
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id = 'ouroboros'")
    return int(row["id"])


async def _prior(pool, game_id: int, errors: list[str | None]) -> None:
    """Earlier prefill jobs for the game, oldest first. None = succeeded."""
    for err in errors:
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source, started_at, "
            "finished_at, error) VALUES ('prefill', ?, 'epic', ?, 'scheduler', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)",
            (game_id, "failed" if err else "succeeded", err),
        )


async def _run(pool, game_id: int, message: str, status_code: int | None = 404) -> None:
    job_id = await pool.execute_write(
        "INSERT INTO jobs (kind, game_id, platform, state, source, started_at) "
        "VALUES ('prefill', ?, 'epic', 'running', 'scheduler', CURRENT_TIMESTAMP)",
        (game_id,),
    )
    row = await pool.read_one("SELECT max(id) AS id FROM jobs")
    job = {"id": int(row["id"]), "kind": "prefill", "game_id": game_id, "platform": "epic"}
    assert job_id is not None
    with pytest.raises(EpicManifestError):
        await prefill_handler(
            job, Deps(pool=pool, epic_client=_FailingEpicClient(message, status_code))
        )


async def _blocked(pool) -> bool:
    row = await pool.read_one(
        "SELECT source, reason FROM block_list WHERE platform='epic' AND app_id='ouroboros'"
    )
    return row is not None


async def test_the_tenth_consecutive_4xx_blocks_the_game(pool):
    game_id = await _game(pool)
    await _prior(pool, game_id, [_API_404] * 5 + [_DL_403] * 4)

    await _run(pool, game_id, "epic manifest API failed: HTTP 404")

    row = await pool.read_one(
        "SELECT source, reason FROM block_list WHERE platform='epic' AND app_id='ouroboros'"
    )
    assert row is not None, "10 consecutive manifest 4xx must block the game"
    assert row["source"] == "api"
    assert "#388" in row["reason"]


async def test_nine_are_not_enough(pool):
    game_id = await _game(pool)
    await _prior(pool, game_id, [_API_404] * 8)

    await _run(pool, game_id, "epic manifest API failed: HTTP 404")

    assert not await _blocked(pool)


async def test_a_success_in_between_resets_the_streak(pool):
    game_id = await _game(pool)
    await _prior(pool, game_id, [_API_404] * 5 + [None] + [_API_404] * 4)

    await _run(pool, game_id, "epic manifest API failed: HTTP 404")

    assert not await _blocked(pool)


async def test_a_different_error_in_between_resets_the_streak(pool):
    game_id = await _game(pool)
    await _prior(
        pool,
        game_id,
        [_API_404] * 5 + ["EpicManifestError: bad manifest magic: 0x614d227b"] + [_API_404] * 4,
    )

    await _run(pool, game_id, "epic manifest API failed: HTTP 404")

    assert not await _blocked(pool)


@pytest.mark.parametrize("code", [429, 401])
async def test_rate_limits_and_auth_failures_never_count(pool, code):
    """Ten in a row of either must not block: both are transient."""
    game_id = await _game(pool)
    await _prior(pool, game_id, [f"EpicManifestError: epic manifest API failed: HTTP {code}"] * 9)

    await _run(pool, game_id, f"epic manifest API failed: HTTP {code}", status_code=code)

    assert not await _blocked(pool)
