"""Tests for orchestrator.validator.disk_stat (F7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.worker import Deps
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)
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


async def test_validate_chunks_uses_dedicated_cache_stat_pool(tmp_path, monkeypatch):
    """#123.4: cache stat I/O must run on a dedicated bounded executor, NOT the
    shared default ThreadPoolExecutor. asyncio also uses the default pool for
    stdlib offloads like getaddrinfo (DNS), so a hung NFS cache mount filling the
    default pool would stall the orchestrator's HTTP probes (lancache heartbeat,
    Epic API). Isolating cache stats bounds the blast radius to validation."""
    import threading

    from orchestrator.validator import disk_stat

    seen_threads: list[str] = []
    real_stat_batch = disk_stat._stat_batch

    def recording_stat_batch(paths):
        seen_threads.append(threading.current_thread().name)
        return real_stat_batch(paths)

    monkeypatch.setattr(disk_stat, "_stat_batch", recording_stat_batch)
    f = tmp_path / "chunk"
    f.write_bytes(b"data")

    await validate_chunks([f])

    assert seen_threads, "no stat batch ran"
    assert all(name.startswith("cache-stat") for name in seen_threads), (
        f"stats ran on the shared default pool, not the dedicated one: {seen_threads}"
    )


async def test_shutdown_cache_stat_executor_is_idempotent_and_recreates(tmp_path):
    """#123.4: the lifespan teardown calls shutdown_cache_stat_executor(); it must
    be safe to call when the pool was never created and twice in a row, and a
    later validation must transparently re-create the pool."""
    from orchestrator.validator import disk_stat

    # Never-created + double shutdown: no error.
    disk_stat.shutdown_cache_stat_executor()
    disk_stat.shutdown_cache_stat_executor()
    assert disk_stat._cache_stat_executor is None

    # A validation after shutdown re-creates the pool and still works.
    f = tmp_path / "chunk"
    f.write_bytes(b"data")
    cached, missing = await validate_chunks([f])
    assert (cached, missing) == (1, 0)
    assert disk_stat._cache_stat_executor is not None

    disk_stat.shutdown_cache_stat_executor()


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


# --- validate_game agent seam ------------------------------------------


class _FakeAgent:
    """Records the hashes passed to stat() and returns fixed counts."""

    def __init__(self, *, cached: int, missing: int):
        self._counts = {"cached": cached, "missing": missing}
        self.calls: list[list[str]] = []

    async def stat(self, hashes: list[str]) -> dict[str, int]:
        self.calls.append(list(hashes))
        return dict(self._counts)


def _agent_settings(tmp_path: Path) -> Settings:
    # agent_enabled=True; cache path points at a NON-existent dir to prove the
    # is_dir() guard is bypassed when the agent owns the filesystem.
    return Settings(
        orchestrator_token=VALID_TOKEN,
        agent_enabled=True,
        lancache_nginx_cache_path=tmp_path / "no-cache-mount-here",
    )


def _expected_hashes(depot_id: int, shas: list[str]) -> list[str]:
    """Compute the control-side cache-key hashes the SAME way validate_game does.

    This is the drift guard: if validate_game ever changes how it derives the
    hash from (identifier, depot_id, sha, slice_range), this list diverges and
    the agent-path test fails.
    """
    slice_range = slice_range_zero(10_485_760)
    out: list[str] = []
    seen: set[str] = set()
    for sha in shas:
        if sha in seen:
            continue
        seen.add(sha)
        out.append(cache_key("steam", steam_chunk_uri(depot_id, sha), slice_range))
    return out


async def test_agent_path_uses_stat_and_not_validate_chunks(pool, tmp_path, monkeypatch):
    """Flag-ON: validate_game must derive the control-side hashes, hand them to
    deps.agent_client.stat(), and NOT touch the in-process validate_chunks path
    (which needs a mounted cache the control plane may not have)."""
    from orchestrator.validator import disk_stat

    async def _boom(*_a, **_k):
        raise AssertionError("validate_chunks must NOT be called on the agent path")

    monkeypatch.setattr(disk_stat, "validate_chunks", _boom)

    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    agent = _FakeAgent(cached=2, missing=3)
    deps = Deps(
        pool=pool,
        steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}),
        agent_client=agent,
    )

    result = await validate_game(pool, deps, game_id, _agent_settings(tmp_path))

    # (1) drift guard: the recorded hashes equal the control-side computation.
    assert agent.calls == [_expected_hashes(731, [SHA_A, SHA_B])]
    # (2) counts come straight from the agent; total = cached + missing.
    assert (result.chunks_cached, result.chunks_missing, result.chunks_total) == (2, 3, 5)
    # (3) outcome classified from agent counts (2 cached of 5 -> partial).
    assert result.outcome == "partial"


async def test_agent_path_classifies_all_cached(pool, tmp_path, monkeypatch):
    """All chunks cached per the agent -> outcome 'cached'."""
    from orchestrator.validator import disk_stat

    async def _boom(*_a, **_k):
        raise AssertionError("validate_chunks must NOT be called on the agent path")

    monkeypatch.setattr(disk_stat, "validate_chunks", _boom)

    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    agent = _FakeAgent(cached=2, missing=0)
    deps = Deps(
        pool=pool,
        steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}),
        agent_client=agent,
    )

    result = await validate_game(pool, deps, game_id, _agent_settings(tmp_path))
    assert result.outcome == "cached"
    assert (result.chunks_cached, result.chunks_missing, result.chunks_total) == (2, 0, 2)


async def test_agent_enabled_but_no_agent_client_is_error(pool, tmp_path):
    """agent_enabled=True but deps.agent_client is None must yield a clean
    error result, not crash."""
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    deps = Deps(
        pool=pool,
        steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A]}),
        agent_client=None,
    )
    result = await validate_game(pool, deps, game_id, _agent_settings(tmp_path))
    assert result.outcome == "error"
    assert result.error is not None


# --- validate_game steam_validate_via_agent (③a-6) ---------------------


class _FakeAgentSV:
    """Records steam_validate(app_id) calls and returns a fixed validate dict."""

    def __init__(self):
        self.calls: list[int] = []

    async def steam_validate(self, app_id: int) -> dict:
        self.calls.append(app_id)
        return {
            "chunks_total": 60,
            "chunks_cached": 55,
            "chunks_missing": 5,
            "outcome": "partial",
            "versions": "1018131:x",
            "error": None,
        }


def _validate_via_agent_settings(tmp_path: Path) -> Settings:
    # steam_validate_via_agent=True; cache path points at a NON-existent dir to
    # prove the legacy is_dir() guard / manifest path is never reached.
    return Settings(
        orchestrator_token=VALID_TOKEN,
        steam_validate_via_agent=True,
        lancache_nginx_cache_path=tmp_path / "no-cache-mount-here",
    )


async def test_validate_via_agent_delegates_and_skips_legacy(pool, tmp_path):
    """Flag-ON (steam_validate_via_agent): validate_game must look up the game's
    app_id, call deps.agent_client.steam_validate(int(app_id)), map the dict to a
    ValidationResult, and NEVER touch the legacy worker manifest_expand path."""
    game_id = await _seed_game(pool, app_id="1018130")
    # A manifest exists, but the legacy path must NOT be exercised.
    await _seed_manifest(pool, game_id, depot_id=731, version="100")

    async def _boom_expand(*_a, **_k):
        raise AssertionError("manifest_expand must NOT be called on the validate-via-agent path")

    steam = _StubSteam({"depot_id": 731, "chunk_shas": [SHA_A]})
    steam.manifest_expand = _boom_expand  # type: ignore[method-assign]
    agent = _FakeAgentSV()
    deps = Deps(pool=pool, steam_client=steam, agent_client=agent)

    result = await validate_game(pool, deps, game_id, _validate_via_agent_settings(tmp_path))

    # (1) steam_validate called once with the int-coerced app_id.
    assert agent.calls == [1018130]
    # (2) manifest_expand NOT called (no recorded calls; _boom would have raised).
    assert steam.calls == []
    # (3) dict mapped onto ValidationResult.
    assert result.chunks_total == 60
    assert result.chunks_cached == 55
    assert result.chunks_missing == 5
    assert result.outcome == "partial"
    assert result.manifest_version == "1018131:x"
    assert result.error is None


async def test_validate_via_agent_no_agent_client_is_error(pool, tmp_path):
    """Flag-ON but deps.agent_client is None must yield a clean error result,
    not crash."""
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id, depot_id=731, version="100")
    deps = Deps(
        pool=pool,
        steam_client=_StubSteam({"depot_id": 731, "chunk_shas": [SHA_A]}),
        agent_client=None,
    )
    result = await validate_game(pool, deps, game_id, _validate_via_agent_settings(tmp_path))
    assert result.outcome == "error"
    assert result.error is not None
