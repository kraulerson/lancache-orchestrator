"""Tests for orchestrator.jobs.handlers.validate (F7).

Validation is delegated to the data-plane agent's /v1/steam/validate (re-arch
③). These tests stub ``agent_client.steam_validate`` with a canned outcome and
assert the handler's DB effects (validation_history row + games.status).
"""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.validate import validate_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


class _StubAgent:
    """Stand-in for AgentClient — returns a canned steam_validate response."""

    def __init__(self, response):
        self._response = response
        self.calls: list[int] = []

    async def steam_validate(self, app_id: int):
        self.calls.append(app_id)
        return self._response


def _vresp(total, cached, missing, outcome, *, versions="731:100", error=None):
    return {
        "chunks_total": total,
        "chunks_cached": cached,
        "chunks_missing": missing,
        "outcome": outcome,
        "versions": versions,
        "error": error,
    }


def _job(game_id: int, platform: str = "steam") -> dict:
    return {"id": 1, "kind": "validate", "platform": platform, "game_id": game_id}


async def _seed_game(pool, *, platform="steam", app_id="730") -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) VALUES (?, ?, 't', 1)",
        (platform, app_id),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


async def test_cached_marks_up_to_date(pool):
    game_id = await _seed_game(pool)
    deps = Deps(pool=pool, agent_client=_StubAgent(_vresp(2, 2, 0, "cached")))
    await validate_handler(_job(game_id), deps)

    vh = await pool.read_one(
        "SELECT method, outcome, chunks_total, chunks_cached "
        "FROM validation_history WHERE game_id=?",
        (game_id,),
    )
    assert vh["method"] == "disk_stat"
    assert vh["outcome"] == "cached"
    assert vh["chunks_total"] == 2
    g = await pool.read_one("SELECT status, last_validated_at FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"
    assert g["last_validated_at"] is not None


async def test_validate_one_game_returns_result_and_records(pool):
    """F13: the extracted helper validates one game, records a validation_history
    row, updates status, and returns the ValidationResult."""
    from orchestrator.core.settings import get_settings
    from orchestrator.jobs.handlers.validate import validate_one_game

    game_id = await _seed_game(pool)
    deps = Deps(pool=pool, agent_client=_StubAgent(_vresp(2, 2, 0, "cached")))

    result = await validate_one_game(pool, deps, game_id, get_settings())

    assert result.outcome == "cached"
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"
    vh = await pool.read_one("SELECT outcome FROM validation_history WHERE game_id=?", (game_id,))
    assert vh["outcome"] == "cached"


async def test_missing_marks_validation_failed(pool):
    game_id = await _seed_game(pool)
    deps = Deps(pool=pool, agent_client=_StubAgent(_vresp(1, 0, 1, "missing")))
    await validate_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "validation_failed"
    vh = await pool.read_one("SELECT outcome FROM validation_history WHERE game_id=?", (game_id,))
    assert vh["outcome"] == "missing"


async def test_partial_marks_validation_failed(pool):
    game_id = await _seed_game(pool)
    deps = Deps(pool=pool, agent_client=_StubAgent(_vresp(2, 1, 1, "partial")))
    await validate_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "validation_failed"


async def test_error_does_not_clobber_classified_status(pool):
    # The agent returns an 'error' outcome (e.g. no manifest in cache).
    game_id = await _seed_game(pool)
    # A real, already-classified status must NOT be clobbered by an error outcome.
    await pool.execute_write("UPDATE games SET status='up_to_date' WHERE id=?", (game_id,))
    deps = Deps(
        pool=pool,
        agent_client=_StubAgent(
            _vresp(0, 0, 0, "error", versions="", error="no_manifest_in_cache")
        ),
    )
    await validate_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"  # unchanged
    vh = await pool.read_one("SELECT outcome FROM validation_history WHERE game_id=?", (game_id,))
    assert vh["outcome"] == "error"


async def test_error_unsticks_transient_downloading(pool):
    """A post-prefill validate that hits an infra error must resolve the transient
    'downloading' state to 'failed', not leave it stuck (UAT-10 #3). It still must
    not clobber a real classified status (above)."""
    game_id = await _seed_game(pool)
    await pool.execute_write("UPDATE games SET status='downloading' WHERE id=?", (game_id,))
    deps = Deps(
        pool=pool,
        agent_client=_StubAgent(_vresp(0, 0, 0, "error", versions="", error="agent unreachable")),
    )
    await validate_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "failed"  # transient 'downloading' resolved, not stuck


async def test_unknown_platform_raises(pool):
    """An unrecognised platform (not steam or epic) still raises ValueError."""
    game_id = await _seed_game(pool)
    deps = Deps(pool=pool, agent_client=_StubAgent(_vresp(0, 0, 0, "error")))
    with pytest.raises(ValueError):
        await validate_handler(_job(game_id, platform="playstation"), deps)


class _StubEpicAgent:
    """Stand-in for AgentClient — returns a canned epic_validate response."""

    def __init__(self, response):
        self._response = response
        self.calls: list[int] = []

    async def epic_validate(
        self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str
    ) -> dict:
        self.calls.append(app_id)
        return self._response


async def test_epic_platform_does_not_raise(pool):
    """After the fix, validate_handler accepts platform='epic' without raising.
    It routes through validate_game → _validate_epic_game → epic_validate."""
    game_id = await _seed_game(pool, platform="epic", app_id="12345")
    await pool.execute_write(
        "INSERT INTO manifests (game_id, version, raw, chunk_count, total_bytes, cdn_base) "
        "VALUES (?, 'v1', ?, 0, 0, 'https://cdn.epicgames.com')",
        (game_id, b"manifest"),
    )
    agent = _StubEpicAgent(
        {
            "chunks_total": 5,
            "chunks_cached": 5,
            "chunks_missing": 0,
            "outcome": "cached",
            "versions": "v1",
            "error": None,
        }
    )
    deps = Deps(pool=pool, agent_client=agent)
    # Must not raise; validates successfully and records a validation_history row.
    await validate_handler(_job(game_id, platform="epic"), deps)
    vh = await pool.read_one("SELECT outcome FROM validation_history WHERE game_id=?", (game_id,))
    assert vh is not None
    assert vh["outcome"] == "cached"


async def test_unknown_game_raises(pool):
    deps = Deps(pool=pool, agent_client=_StubAgent(_vresp(0, 0, 0, "error")))
    with pytest.raises(ValueError, match="not found"):
        await validate_handler(_job(99999), deps)


async def test_validate_handler_registered():
    from orchestrator.jobs.handlers import HANDLERS, _register_builtin_handlers

    _register_builtin_handlers()
    assert "validate" in HANDLERS


async def test_validate_never_writes_cached_version(pool, monkeypatch):
    """F8 prefill-sole-writer: validate updates status but NEVER cached_version,
    even on a clean outcome (a standalone sweep may validate a stale manifest)."""
    from orchestrator.core.settings import get_settings
    from orchestrator.jobs.handlers.validate import validate_one_game
    from orchestrator.validator.disk_stat import ValidationResult

    game_id = await _seed_game(pool)
    await pool.execute_write(
        "UPDATE games SET current_version='42', cached_version='OLD', status='unknown' WHERE id=?",
        (game_id,),
    )

    async def fake_validate(p, d, gid, s):
        return ValidationResult(3, 3, 0, "cached", "42")

    monkeypatch.setattr("orchestrator.jobs.handlers.validate.validate_game", fake_validate)
    await validate_one_game(pool, Deps(pool=pool), game_id, get_settings())
    row = await pool.read_one("SELECT status, cached_version FROM games WHERE id=?", (game_id,))
    assert row["status"] == "up_to_date"  # status still updates
    assert row["cached_version"] == "OLD"  # cached_version untouched by validate
