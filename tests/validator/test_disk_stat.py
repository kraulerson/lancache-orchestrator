"""Tests for orchestrator.validator.disk_stat (F7)."""

from __future__ import annotations

import base64

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.worker import Deps
from orchestrator.validator.disk_stat import validate_chunks, validate_chunks_any, validate_game

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


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


# --- validate_chunks_any -----------------------------------------------


def _mk(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


async def test_validate_chunks_any_counts_present_under_any_candidate(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    _mk(b)  # chunk1: only its 2nd candidate exists -> cached
    # chunk2: neither candidate exists -> missing
    result = await validate_chunks_any([[a, b], [c, tmp_path / "d"]])
    assert result == (1, 1)  # (cached, present): chunk1 hits via b, chunk2 misses


async def test_validate_chunks_any_empty(tmp_path):
    assert await validate_chunks_any([]) == (0, 0)


async def test_validate_chunks_any_present_size_zero_not_cached(tmp_path):
    """A candidate that exists on disk but is 0 bytes counts as present but NOT
    cached — an empty file has no real content, so it must not count as a hit."""
    p = tmp_path / "empty"
    p.write_bytes(b"")
    result = await validate_chunks_any([[p]])
    assert result == (0, 1)  # present=1, cached=0


async def test_validate_chunks_any_present_mode000_not_cached(tmp_path):
    """A non-empty candidate file with mode 000 is present but NOT cached (#76/#128).
    The owner-read bit must be set for lancache to serve it; mode-000 files exist
    on disk but lancache cannot read them, so they must not count as cached.
    If the test runner is root, chmod 000 may still be readable — skip in that case."""
    import os

    f = tmp_path / "mode000"
    f.write_bytes(b"data")
    os.chmod(f, 0o000)
    try:
        result = await validate_chunks_any([[f]])
    finally:
        os.chmod(f, 0o644)  # restore so tmp cleanup can remove it
    # If running as root, stat() sees size>0 and mode 000 but root bypasses mode checks —
    # the kernel sets all mode bits for root, so 0o400 would appear set. Skip that case.
    if result == (1, 1):
        pytest.skip("test runner is root; chmod 000 is readable, skipping mode-000 sub-case")
    assert result == (0, 1)  # present=1, cached=0


async def test_validate_chunks_any_symlink_skipped(tmp_path):
    """A candidate that is a symlink to a real non-empty file is skipped — symlinks
    are never genuine nginx cache files. The chunk counts as neither present nor
    cached, so the result is (0, 0)."""
    target = tmp_path / "real_file"
    target.write_bytes(b"content")
    link = tmp_path / "symlink_chunk"
    link.symlink_to(target)
    result = await validate_chunks_any([[link]])
    assert result == (0, 0)  # symlink skipped → not present, not cached


# --- validate_game (delegates to the agent's steam_validate) -----------


class _FakeAgentSV:
    """Records steam_validate(app_id) calls and returns a fixed validate dict."""

    def __init__(self, response):
        self._response = response
        self.calls: list[int] = []

    async def steam_validate(self, app_id: int) -> dict:
        self.calls.append(app_id)
        return self._response


def _settings() -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN)


async def _seed_game(pool, *, platform="steam", app_id="730") -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) VALUES (?, ?, 't', 1)",
        (platform, app_id),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


async def test_validate_delegates_to_agent_and_maps_result(pool):
    """validate_game resolves the game's app_id, calls agent.steam_validate with
    the int-coerced id, and maps the returned dict onto a ValidationResult."""
    game_id = await _seed_game(pool, app_id="1018130")
    agent = _FakeAgentSV(
        {
            "chunks_total": 60,
            "chunks_cached": 55,
            "chunks_missing": 5,
            "outcome": "partial",
            "versions": "1018131:x",
            "error": None,
        }
    )
    deps = Deps(pool=pool, agent_client=agent)

    result = await validate_game(pool, deps, game_id, _settings())

    assert agent.calls == [1018130]
    assert result.chunks_total == 60
    assert result.chunks_cached == 55
    assert result.chunks_missing == 5
    assert result.outcome == "partial"
    assert result.manifest_version == "1018131:x"
    assert result.error is None


async def test_validate_no_agent_client_is_error(pool):
    """deps.agent_client is None must yield a clean error result, not crash."""
    game_id = await _seed_game(pool)
    deps = Deps(pool=pool, agent_client=None)
    result = await validate_game(pool, deps, game_id, _settings())
    assert result.outcome == "error"
    assert result.error is not None


async def test_validate_unknown_game_is_error(pool):
    agent = _FakeAgentSV({"chunks_total": 0})
    deps = Deps(pool=pool, agent_client=agent)
    result = await validate_game(pool, deps, 99999, _settings())
    assert result.outcome == "error"
    assert agent.calls == []  # never reached the agent


async def test_validate_nonnumeric_app_id_is_error(pool):
    game_id = await _seed_game(pool, app_id="not-a-number")
    agent = _FakeAgentSV({"chunks_total": 0})
    deps = Deps(pool=pool, agent_client=agent)
    result = await validate_game(pool, deps, game_id, _settings())
    assert result.outcome == "error"
    assert agent.calls == []  # bad app_id rejected before the agent call


# --- validate_game — Epic platform dispatch --------------------------------


class _FakeAgentEV:
    """Records epic_validate keyword-arg calls and returns a fixed validate dict."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def epic_validate(
        self,
        *,
        app_id: int,
        version: str,
        cdn_base: str,
        raw_manifest_b64: str,
    ) -> dict:
        self.calls.append(
            {
                "app_id": app_id,
                "version": version,
                "cdn_base": cdn_base,
                "raw_manifest_b64": raw_manifest_b64,
            }
        )
        return self._response


async def _seed_manifest(
    pool,
    game_id: int,
    *,
    version: str = "1.0",
    cdn_base: str | None = "https://cdn.example.com",
    raw: bytes = b"manifest-raw",
) -> None:
    await pool.execute_write(
        "INSERT INTO manifests (game_id, version, raw, chunk_count, total_bytes, cdn_base) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        (game_id, version, raw, cdn_base),
    )


async def test_validate_epic_calls_agent_and_maps_result(pool):
    """validate_game for an epic game reads the stored manifest, b64-encodes raw,
    and forwards app_id + version + cdn_base + raw_manifest_b64 to epic_validate.
    The returned dict is shaped into a ValidationResult identical to the steam path."""
    raw = b"epic-raw-manifest"
    game_id = await _seed_game(pool, platform="epic", app_id="12345")
    await _seed_manifest(
        pool, game_id, version="v1.2", cdn_base="https://cdn.epicgames.com", raw=raw
    )
    agent = _FakeAgentEV(
        {
            "chunks_total": 10,
            "chunks_cached": 8,
            "chunks_missing": 2,
            "outcome": "partial",
            "versions": "v1.2",
            "error": None,
        }
    )
    deps = Deps(pool=pool, agent_client=agent)

    result = await validate_game(pool, deps, game_id, _settings())

    expected_b64 = base64.b64encode(raw).decode("ascii")
    assert len(agent.calls) == 1
    call = agent.calls[0]
    assert call["app_id"] == 12345
    assert call["version"] == "v1.2"
    assert call["cdn_base"] == "https://cdn.epicgames.com"
    assert call["raw_manifest_b64"] == expected_b64
    assert result.chunks_total == 10
    assert result.chunks_cached == 8
    assert result.chunks_missing == 2
    assert result.outcome == "partial"
    assert result.manifest_version == "v1.2"


async def test_validate_epic_no_cdn_base_is_error(pool):
    """Epic manifest with NULL cdn_base → error='no_cdn_base'; agent NOT called.
    Affects pre-migration rows (re-prefill heals by writing cdn_base).
    The real manifest version is returned (not '') so callers have context."""
    game_id = await _seed_game(pool, platform="epic", app_id="99001")
    await _seed_manifest(pool, game_id, cdn_base=None)  # version defaults to "1.0"
    agent = _FakeAgentEV({})
    deps = Deps(pool=pool, agent_client=agent)

    result = await validate_game(pool, deps, game_id, _settings())

    assert result.outcome == "error"
    assert result.error == "no_cdn_base"
    assert agent.calls == []
    assert result.manifest_version == "1.0"  # intentional: returns real version, not ""


async def test_validate_epic_no_manifest_is_error(pool):
    """Epic game with no manifests row → error='no_manifest'; agent NOT called."""
    game_id = await _seed_game(pool, platform="epic", app_id="99002")
    # No manifest seeded intentionally.
    agent = _FakeAgentEV({})
    deps = Deps(pool=pool, agent_client=agent)

    result = await validate_game(pool, deps, game_id, _settings())

    assert result.outcome == "error"
    assert result.error == "no_manifest"
    assert agent.calls == []
