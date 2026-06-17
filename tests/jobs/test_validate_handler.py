"""Tests for orchestrator.jobs.handlers.validate (F7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from orchestrator.jobs.handlers.validate import validate_handler
from orchestrator.jobs.worker import Deps
from orchestrator.validator.cache_key import cache_key, cache_path, slice_range_zero

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

SHA_A = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"
SHA_B = "234a47ed3005727db220987ecac460030295bd79"


class _StubSteam:
    def __init__(self, response):
        self._response = response

    async def manifest_expand(self, raw: bytes):
        return self._response


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


async def _seed_manifest(pool, game_id, *, depot_id=731, version="100"):
    await pool.execute_write(
        "INSERT INTO manifests "
        "(game_id, depot_id, version, fetched_at, chunk_count, total_bytes, raw) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, 100, ?)",
        (game_id, depot_id, version, b"BLOB"),
    )


def _make_cache_file(root: Path, depot_id: int, sha: str):
    uri = f"/depot/{depot_id}/chunk/{sha}"
    h = cache_key("steam", uri, slice_range_zero(10_485_760))
    p = cache_path(root, h, "2:2")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"data")


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Point Settings.lancache_nginx_cache_path at a tmp cache tree."""
    monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(tmp_path))
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_cached_marks_up_to_date(pool, cache_root):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)
    _make_cache_file(cache_root, 731, SHA_A)
    _make_cache_file(cache_root, 731, SHA_B)
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}))
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


async def test_validate_one_game_returns_result_and_records(pool, cache_root):
    """F13: the extracted helper validates one game, records a validation_history
    row, updates status, and returns the ValidationResult."""
    from orchestrator.core.settings import get_settings
    from orchestrator.jobs.handlers.validate import validate_one_game

    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)
    _make_cache_file(cache_root, 731, SHA_A)
    _make_cache_file(cache_root, 731, SHA_B)
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}))

    result = await validate_one_game(pool, deps, game_id, get_settings())

    assert result.outcome == "cached"
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"
    vh = await pool.read_one("SELECT outcome FROM validation_history WHERE game_id=?", (game_id,))
    assert vh["outcome"] == "cached"


async def test_missing_marks_validation_failed(pool, cache_root):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A]}))
    await validate_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "validation_failed"
    vh = await pool.read_one("SELECT outcome FROM validation_history WHERE game_id=?", (game_id,))
    assert vh["outcome"] == "missing"


async def test_partial_marks_validation_failed(pool, cache_root):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)
    _make_cache_file(cache_root, 731, SHA_A)
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}))
    await validate_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "validation_failed"


async def test_error_does_not_clobber_classified_status(pool, tmp_path, monkeypatch):
    # Point at a non-existent cache root → outcome error.
    monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(tmp_path / "nope"))
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)
    # A real, already-classified status must NOT be clobbered by an error outcome.
    await pool.execute_write("UPDATE games SET status='up_to_date' WHERE id=?", (game_id,))
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A]}))
    await validate_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"  # unchanged
    vh = await pool.read_one("SELECT outcome FROM validation_history WHERE game_id=?", (game_id,))
    assert vh["outcome"] == "error"
    get_settings.cache_clear()


async def test_error_unsticks_transient_downloading(pool, tmp_path, monkeypatch):
    """A post-prefill validate that hits an infra error (cache unmounted) must
    resolve the transient 'downloading' state to 'failed', not leave it stuck
    (UAT-10 #3). It still must not clobber a real classified status (above)."""
    monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(tmp_path / "nope"))
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)
    await pool.execute_write("UPDATE games SET status='downloading' WHERE id=?", (game_id,))
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A]}))
    await validate_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "failed"  # transient 'downloading' resolved, not stuck
    get_settings.cache_clear()


async def test_non_steam_raises(pool):
    game_id = await _seed_game(pool, platform="epic", app_id="fort")
    deps = Deps(pool=pool, steam_client=_StubSteam({}))
    with pytest.raises(ValueError, match="steam"):
        await validate_handler(_job(game_id, platform="epic"), deps)


async def test_unknown_game_raises(pool, cache_root):
    deps = Deps(pool=pool, steam_client=_StubSteam({}))
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
    await validate_one_game(
        pool, Deps(pool=pool, steam_client=_StubSteam(None)), game_id, get_settings()
    )
    row = await pool.read_one("SELECT status, cached_version FROM games WHERE id=?", (game_id,))
    assert row["status"] == "up_to_date"  # status still updates
    assert row["cached_version"] == "OLD"  # cached_version untouched by validate
