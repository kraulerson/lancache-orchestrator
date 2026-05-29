"""Tests for orchestrator.validator.disk_stat (F7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.worker import Deps
from orchestrator.validator.cache_key import cache_key, cache_path, slice_range_zero
from orchestrator.validator.disk_stat import validate_chunks, validate_game

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32
SHA_A = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"
SHA_B = "234a47ed3005727db220987ecac460030295bd79"


# --- validate_chunks ---------------------------------------------------


async def test_counts_cached_and_missing(tmp_path):
    present = tmp_path / "a"
    present.write_bytes(b"x")
    empty = tmp_path / "b"
    empty.write_bytes(b"")
    absent = tmp_path / "c"
    cached, missing = await validate_chunks([present, empty, absent])
    assert (cached, missing) == (1, 2)  # empty file counts as missing


async def test_batch_boundary(tmp_path):
    paths = []
    for i in range(300):
        p = tmp_path / f"f{i}"
        p.write_bytes(b"x")
        paths.append(p)
    cached, missing = await validate_chunks(paths, batch_size=256)
    assert (cached, missing) == (300, 0)


async def test_empty_path_list(tmp_path):
    assert await validate_chunks([]) == (0, 0)


async def test_unreadable_mode000_not_counted(tmp_path):
    """F5: a mode-000 cache file is unreadable by lancache (owner www-data has
    no read bit); it must NOT count as cached even though it exists size>0."""
    import os

    f = tmp_path / "unreadable"
    f.write_bytes(b"data")
    os.chmod(f, 0o000)
    try:
        cached, missing = await validate_chunks([f])
    finally:
        os.chmod(f, 0o644)  # restore so tmp cleanup can remove it
    assert (cached, missing) == (0, 1)


async def test_readable_mode644_counted(tmp_path):
    import os

    f = tmp_path / "readable"
    f.write_bytes(b"data")
    os.chmod(f, 0o644)
    cached, missing = await validate_chunks([f])
    assert (cached, missing) == (1, 0)


async def test_symlink_not_counted_cached(tmp_path):
    """Bug E: stat must not follow symlinks — a cache path that is a symlink
    to an unrelated non-empty file is NOT a real cached chunk."""
    target = tmp_path / "elsewhere"
    target.write_bytes(b"unrelated content")
    link = tmp_path / "chunkpath"
    link.symlink_to(target)
    cached, missing = await validate_chunks([link])
    assert (cached, missing) == (0, 1)


# --- validate_game helpers ---------------------------------------------


class _StubSteam:
    """manifest_expand returns a fixed depot_id + chunk_shas per call."""

    def __init__(self, response):
        self._response = response
        self.calls: list[bytes] = []

    async def manifest_expand(self, raw: bytes):
        self.calls.append(raw)
        return self._response


def _settings(tmp_path: Path) -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=tmp_path)


async def _seed_game(pool, *, platform="steam", app_id="730") -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) VALUES (?, ?, 't', 1)",
        (platform, app_id),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


async def _seed_manifest(pool, game_id, *, depot_id, version, raw=b"BLOB"):
    await pool.execute_write(
        "INSERT INTO manifests "
        "(game_id, depot_id, version, fetched_at, chunk_count, total_bytes, raw) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, 100, ?)",
        (game_id, depot_id, version, raw),
    )


def _make_cache_file(root: Path, depot_id: int, sha: str, content=b"data"):
    """Create the cache file at the path validate_game will compute."""
    uri = f"/depot/{depot_id}/chunk/{sha}"
    h = cache_key("steam", uri, slice_range_zero(10_485_760))
    p = cache_path(root, h, "2:2")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


# --- validate_game -----------------------------------------------------


async def test_cached_when_all_chunks_present(pool, tmp_path):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    _make_cache_file(tmp_path, 731, SHA_A)
    _make_cache_file(tmp_path, 731, SHA_B)
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}))
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.outcome == "cached"
    assert (result.chunks_total, result.chunks_cached, result.chunks_missing) == (2, 2, 0)


async def test_partial_when_some_missing(pool, tmp_path):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    _make_cache_file(tmp_path, 731, SHA_A)  # only A present
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}))
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.outcome == "partial"
    assert (result.chunks_total, result.chunks_cached, result.chunks_missing) == (2, 1, 1)


async def test_missing_when_none_present(pool, tmp_path):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}))
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.outcome == "missing"
    assert (result.chunks_total, result.chunks_cached, result.chunks_missing) == (2, 0, 2)


async def test_dedup_across_mappings(pool, tmp_path):
    """Worker may return duplicate SHAs; validate_game dedups by (depot, sha)."""
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    _make_cache_file(tmp_path, 731, SHA_A)
    deps = Deps(
        pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_A, SHA_A]})
    )
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.chunks_total == 1
    assert result.outcome == "cached"


async def test_zero_chunk_manifest_is_cached_not_error(pool, tmp_path):
    """Bug B: a valid manifest that expands to zero chunks means nothing to
    cache — that is 'cached' (up_to_date), not an infra 'error'."""
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": []}))
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.outcome == "cached"
    assert (result.chunks_total, result.chunks_cached, result.chunks_missing) == (0, 0, 0)


async def test_no_manifests_is_error(pool, tmp_path):
    game_id = await _seed_game(pool)
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 0, "chunk_shas": []}))
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.outcome == "error"
    assert result.chunks_total == 0


async def test_malformed_sha_is_error_not_raise(pool, tmp_path):
    """Bug C: a malformed chunk SHA from the worker must yield outcome=error,
    not an uncaught ValueError that fails the whole job."""
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": ["NOT_HEX"]}))
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.outcome == "error"
    assert result.error is not None


async def test_depot_id_mismatch_is_error(pool, tmp_path):
    """Bug D: if the worker-parsed depot_id disagrees with the DB column,
    the stored BLOB doesn't match its row — fail closed rather than stat
    wrong paths."""
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 999, "chunk_shas": [SHA_A]}))
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.outcome == "error"


async def test_cache_root_missing_is_error(pool, tmp_path):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    deps = Deps(pool=pool, steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A]}))
    bad = Settings(orchestrator_token=VALID_TOKEN, lancache_nginx_cache_path=tmp_path / "nope")
    result = await validate_game(pool, deps, game_id, bad)
    assert result.outcome == "error"


async def test_latest_manifest_per_depot(pool, tmp_path):
    """Only the most-recent manifest per depot is validated; old gids ignored."""
    game_id = await _seed_game(pool)
    # Older manifest for depot 731 (version 100) then a newer one (version 200).
    await _seed_manifest(pool, game_id, depot_id=731, version="100", raw=b"OLD")
    await _seed_manifest(pool, game_id, depot_id=731, version="200", raw=b"NEW")
    _make_cache_file(tmp_path, 731, SHA_A)
    stub = _StubSteam({"depot_id": 731, "chunk_shas": [SHA_A]})
    deps = Deps(pool=pool, steam_client=stub)
    result = await validate_game(pool, deps, game_id, _settings(tmp_path))
    assert result.outcome == "cached"
    # Only the newest BLOB was expanded (one call, with the NEW bytes).
    assert stub.calls == [b"NEW"]
