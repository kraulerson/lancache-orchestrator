"""A purge must record that the cache changed, not just that the game needs re-prefill.

UAT-14 #293, found by the human tester, who declined to tick a clean pass and noted
the chunk counts still read fully cached after a purge:

    status             ⚠ VALIDATION_FAILED
    chunks_cached      337
    chunks_total       337

``chunks_cached`` is not a column on ``games`` — ``api/routers/games.py`` reads it
from the newest ``validation_history`` row. That row is historically accurate; it
really was 337/337 when it was written. The defect is that purge deletes the files
and records nothing, so the newest observation stays the pre-purge one and every
consumer keeps reporting a fully cached game with no files behind it. Game_shelf's
cache badge is driven by exactly those two fields.

So the fix is not to blank a value, it is to record the observation the purge is in
the best possible position to make: it knows precisely what it deleted.
"""

from __future__ import annotations

import pytest

from orchestrator.clients.agent_client import AgentError
from orchestrator.jobs.handlers.purge import purge_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


class _StubPurgeAgent:
    def __init__(self, *, steam=None, epic=None, raise_exc=None):
        self._steam = steam
        self._epic = epic
        self._raise = raise_exc

    async def steam_purge(self, app_id: int):
        if self._raise is not None:
            raise self._raise
        return self._steam

    async def epic_purge(self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str):
        if self._raise is not None:
            raise self._raise
        return self._epic


def _job(game_id: int, platform: str = "steam") -> dict:
    return {"id": 1, "kind": "purge", "platform": platform, "game_id": game_id}


async def _seed_game(pool, *, platform="steam", app_id="730", status="up_to_date") -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) VALUES (?, ?, 't', 1, ?)",
        (platform, app_id, status),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


async def _seed_validation(pool, game_id: int, *, total: int, cached: int) -> None:
    """The state a healthy validated game is in before anyone purges it."""
    await pool.execute_write(
        "INSERT INTO validation_history (game_id, manifest_version, started_at, finished_at,"
        " method, chunks_total, chunks_cached, chunks_missing, outcome, error) "
        "VALUES (?, 'v1', '2026-08-01T00:00:00Z', '2026-08-01T00:01:00Z', 'disk_stat',"
        " ?, ?, ?, 'cached', NULL)",
        (game_id, total, cached, total - cached),
    )


async def _latest_validation(pool, game_id: int):
    return await pool.read_one(
        "SELECT chunks_total, chunks_cached, chunks_missing, outcome, method "
        "FROM validation_history WHERE game_id = ? ORDER BY id DESC LIMIT 1",
        (game_id,),
    )


async def test_purge_records_that_nothing_is_cached_any_more(pool):
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubPurgeAgent(steam={"deleted": 337, "failed": 0, "bytes_freed": 999})
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 0, (
        "after a purge the newest observation must say nothing is cached. It still reads "
        f"{latest['chunks_cached']}, so the API and the Game_shelf badge keep reporting a "
        "fully cached game whose files were just deleted."
    )
    assert latest["chunks_total"] == 337, "the manifest size is unchanged by a purge"
    assert latest["chunks_missing"] == 337
    assert latest["outcome"] == "missing"


async def test_purge_does_not_rewrite_the_previous_observation(pool):
    """History is append-only: the pre-purge row was true when it was written."""
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubPurgeAgent(steam={"deleted": 337, "failed": 0, "bytes_freed": 999})
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    rows = await pool.read_all(
        "SELECT chunks_cached FROM validation_history WHERE game_id = ? ORDER BY id", (game_id,)
    )
    assert [r["chunks_cached"] for r in rows] == [337, 0], (
        "the original observation must be preserved and a new one appended"
    )


async def test_epic_purge_records_it_too(pool):
    game_id = await _seed_game(pool, platform="epic", app_id="12345")
    await pool.execute_write(
        "INSERT INTO manifests (game_id, version, raw, chunk_count, total_bytes, cdn_base) "
        "VALUES (?, 'v1', ?, 0, 0, 'https://cdn.epicgames.com')",
        (game_id, b"manifest"),
    )
    await _seed_validation(pool, game_id, total=50, cached=50)

    agent = _StubPurgeAgent(epic={"deleted": 50, "failed": 0, "bytes_freed": 42})
    await purge_handler(_job(game_id, platform="epic"), Deps(pool=pool, agent_client=agent))

    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 0
    assert latest["outcome"] == "missing"


async def test_a_failed_purge_records_nothing(pool):
    """If the agent never deleted anything, no observation was made."""
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubPurgeAgent(raise_exc=AgentError("agent unreachable"))
    with pytest.raises(AgentError):
        await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    latest = await _latest_validation(pool, game_id)
    assert latest["chunks_cached"] == 337, (
        "a purge that failed must not claim the cache is empty — that would be the same "
        "false report in the opposite direction"
    )


async def test_purge_with_no_prior_validation_records_nothing(pool):
    """Nothing is known about chunk counts, so inventing a total would be a lie."""
    game_id = await _seed_game(pool, app_id="440")

    agent = _StubPurgeAgent(steam={"deleted": 0, "failed": 0, "bytes_freed": 0})
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    rows = await pool.read_all("SELECT id FROM validation_history WHERE game_id = ?", (game_id,))
    assert rows == [], "with no manifest size known, purge must not fabricate an observation"

    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "validation_failed", "the re-prefill flag is set regardless"


async def test_the_status_and_the_observation_land_together(pool):
    """Flagging for re-prefill and recording the empty cache are one atomic write.

    They were two separate execute_write calls. A crash or PoolError between them
    left the files deleted, status='validation_failed', and the newest
    validation_history row still reading 337/337 cached — exactly the badge #293
    exists to fix, resurrected for up to 6h until the sweep revalidates.

    Asserted by failing the second write and requiring the first to roll back with
    it: under one transaction neither lands, so the job fails visibly with the store
    self-consistent, rather than half-applied.
    """
    game_id = await _seed_game(pool, app_id="440")
    await _seed_validation(pool, game_id, total=337, cached=337)

    agent = _StubPurgeAgent(steam={"deleted": 337, "failed": 0, "bytes_freed": 999})

    import orchestrator.jobs.handlers.purge as purge_mod

    original = purge_mod._record_cache_emptied

    async def boom(pool_arg, gid, tx=None):
        raise RuntimeError("write failed between the two statements")

    purge_mod._record_cache_emptied = boom
    try:
        with pytest.raises(RuntimeError):
            await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))
    finally:
        purge_mod._record_cache_emptied = original

    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date", (
        "the status flip must roll back with the failed observation — otherwise the "
        "game reads validation_failed while the newest row still claims 337/337"
    )
