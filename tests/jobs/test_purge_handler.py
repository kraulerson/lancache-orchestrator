"""Tests for orchestrator.jobs.handlers.purge (F18).

The delete is delegated to the data-plane agent (/v1/{steam,epic}/purge). These
tests stub ``agent_client.steam_purge`` / ``epic_purge`` with a canned result and
assert the handler's DB effect (games.status -> validation_failed) + the
game.purged log event. An AgentError propagates and leaves status unchanged.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from orchestrator.clients.agent_client import AgentError
from orchestrator.jobs.handlers.purge import purge_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


def _measured_empty(total: int = 0) -> dict:
    """What a post-purge validation sees once the chunks are gone.

    Since #310 the purge measures the disk instead of assuming, so every stub
    agent must answer the validate call too — see
    tests/jobs/test_purge_measures_the_disk.py for the cases where that answer is
    the point of the test rather than scaffolding.
    """
    return {
        "chunks_total": total,
        "chunks_cached": 0,
        "chunks_missing": total,
        "outcome": "missing" if total else "error",
        "versions": "v1",
        "error": None if total else "no manifest",
    }


class _StubPurgeAgent:
    """Stand-in for AgentClient — canned steam/epic purge results (or raises)."""

    def __init__(self, *, steam=None, epic=None, raise_exc=None, validate=None):
        self._steam = steam
        self._epic = epic
        self._raise = raise_exc
        self._validate = validate if validate is not None else _measured_empty()
        self.steam_calls: list[int] = []
        self.epic_calls: list[int] = []

    async def steam_purge(self, app_id: int):
        self.steam_calls.append(app_id)
        if self._raise is not None:
            raise self._raise
        return self._steam

    async def epic_purge(self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str):
        self.epic_calls.append(app_id)
        if self._raise is not None:
            raise self._raise
        return self._epic

    async def steam_validate(self, app_id: int, chunk_count=None):
        return self._validate

    async def epic_validate(self, **kwargs):
        return self._validate


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


async def _seed_epic_manifest(pool, game_id, *, cdn_base="https://cdn.epicgames.com"):
    await pool.execute_write(
        "INSERT INTO manifests (game_id, version, raw, chunk_count, total_bytes, cdn_base) "
        "VALUES (?, 'v1', ?, 0, 0, ?)",
        (game_id, b"manifest", cdn_base),
    )


async def test_steam_purge_leaves_the_game_eligible_for_re_prefill(pool):
    """The flip goes through the single writer, so it stamps status_measured_at —
    the evidence the Epic prefill re-queues on.

    Since #310 the status is whatever the post-purge validation measured rather
    than a hard-coded 'partial', so this asserts the invariant that actually
    matters (ADR-0015 reversibility: the game is back in the set F5/F6 select on)
    instead of one particular status string.

    It is still recorded as a COMMANDED change (security audit SEV-2): the files
    are already deleted by the time this runs, so the breaker must not be able to
    refuse the record. What changed in #310 is that the transition row IS now
    written, marked commanded so the breaker's count skips it — a bulk purge used
    to leave no trace at all in the log that exists to record cache truth.
    """
    game_id = await _seed_game(pool, app_id="440")
    agent = _StubPurgeAgent(
        steam={"deleted": 3, "failed": 0, "bytes_freed": 999},
        validate=_measured_empty(total=3),
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))

    assert agent.steam_calls == [440]
    g = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (game_id,))
    assert g["status"] in ("not_downloaded", "validation_failed")
    assert g["status_measured_at"] is not None
    t = await pool.read_all(
        "SELECT prior, new_status, downward, commanded FROM measurement_transitions "
        "WHERE game_id=?",
        (game_id,),
    )
    assert len(t) == 1, "a purge is cache truth and belongs in the log (#310)"
    assert t[0]["commanded"] == 1, "unmarked, it would arm the breaker against the next sweep"


async def test_epic_purge_leaves_the_game_eligible_for_re_prefill(pool):
    game_id = await _seed_game(pool, platform="epic", app_id="12345")
    await _seed_epic_manifest(pool, game_id)
    agent = _StubPurgeAgent(
        epic={"deleted": 5, "failed": 0, "bytes_freed": 42},
        validate=_measured_empty(total=5),
    )
    await purge_handler(_job(game_id, platform="epic"), Deps(pool=pool, agent_client=agent))

    assert agent.epic_calls == [12345]
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] in ("not_downloaded", "validation_failed")


async def test_purging_a_never_cached_game_records_what_the_disk_holds(pool):
    """Idempotence: a never-cached app returns {deleted: 0} and is not an error.

    This asserted 'validation_failed' before #310, which was the handler assuming
    rather than looking. The measurement is what decides now, and for a game with
    no chunks on disk that is 'missing' -> not_downloaded, which is equally in the
    re-prefill set. The distinction matters because {deleted: 0} ALSO describes a
    purge whose every unlink failed against a fully intact cache — the #310 defect
    — and only a real measurement can tell those two apart.
    """
    game_id = await _seed_game(pool, app_id="440")
    agent = _StubPurgeAgent(
        steam={"deleted": 0, "failed": 0, "bytes_freed": 0},
        validate=_measured_empty(total=12),
    )
    await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "not_downloaded"


async def test_agent_error_leaves_status_unchanged(pool):
    game_id = await _seed_game(pool, app_id="440", status="up_to_date")
    agent = _StubPurgeAgent(raise_exc=AgentError("agent unreachable"))
    with pytest.raises(AgentError):
        await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"  # never flipped to validation_failed


async def test_epic_no_manifest_raises_and_leaves_status(pool):
    game_id = await _seed_game(pool, platform="epic", app_id="777", status="up_to_date")
    # No manifest seeded → cannot enumerate → clear error (ADR-0015).
    agent = _StubPurgeAgent(epic={"deleted": 0, "failed": 0, "bytes_freed": 0})
    with pytest.raises(ValueError, match="manifest"):
        await purge_handler(_job(game_id, platform="epic"), Deps(pool=pool, agent_client=agent))
    assert agent.epic_calls == []  # agent never called
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"


async def test_unknown_platform_raises(pool):
    game_id = await _seed_game(pool)
    agent = _StubPurgeAgent(steam={"deleted": 0, "failed": 0, "bytes_freed": 0})
    with pytest.raises(ValueError):
        await purge_handler(
            _job(game_id, platform="playstation"), Deps(pool=pool, agent_client=agent)
        )


async def test_unknown_game_raises(pool):
    agent = _StubPurgeAgent(steam={"deleted": 0, "failed": 0, "bytes_freed": 0})
    with pytest.raises(ValueError, match="not found"):
        await purge_handler(_job(99999), Deps(pool=pool, agent_client=agent))


async def test_logs_game_purged_event(pool):
    game_id = await _seed_game(pool, app_id="440")
    agent = _StubPurgeAgent(steam={"deleted": 3, "failed": 1, "bytes_freed": 42})
    with capture_logs() as logs:
        await purge_handler(_job(game_id), Deps(pool=pool, agent_client=agent))
    events = [e for e in logs if e["event"] == "game.purged"]
    assert len(events) == 1
    ev = events[0]
    assert ev["platform"] == "steam"
    assert ev["files_deleted"] == 3
    assert ev["files_failed"] == 1
    assert ev["total_bytes_freed"] == 42


async def test_purge_handler_registered():
    from orchestrator.jobs.handlers import HANDLERS, _register_builtin_handlers

    _register_builtin_handlers()
    assert "purge" in HANDLERS
