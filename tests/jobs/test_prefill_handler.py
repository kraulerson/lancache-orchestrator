"""Tests for orchestrator.jobs.handlers.prefill (F5)."""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.prefill import prefill_handler
from orchestrator.jobs.worker import Deps
from orchestrator.platform.steam.prefill_driver import PrefillResult as DriverPrefillResult

pytestmark = pytest.mark.asyncio

SHA_A = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"
SHA_B = "234a47ed3005727db220987ecac460030295bd79"


class _StubSteam:
    def __init__(self, expand=None):
        self._expand = expand or {"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}
        self.fetch_calls = 0

    async def manifest_expand(self, raw):
        return self._expand

    async def manifest_fetch(self, app_id):
        self.fetch_calls += 1
        return {"manifests": []}


class _StubDriver:
    """Stand-in for SteamPrefillDriver — records prefill_apps calls."""

    def __init__(self, *, ok=True):
        self._ok = ok
        self.calls: list[tuple[list[int], bool]] = []

    async def prefill_apps(self, app_ids, *, force=False):
        self.calls.append((list(app_ids), force))
        return DriverPrefillResult(ok=self._ok, raw="stub output")


def _job(game_id, platform="steam", **extra):
    return {"id": 1, "kind": "prefill", "platform": platform, "game_id": game_id, **extra}


async def _seed_game(pool, *, platform="steam", app_id="730"):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) VALUES (?, ?, 't', 1)",
        (platform, app_id),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


def _steam_deps(pool, driver):
    """Deps for the rewired steam path: prefill goes through the driver, but
    steam_client is still carried (kept for other handlers)."""
    return Deps(pool=pool, steam_client=_StubSteam(), prefill_driver=driver)


async def test_steam_calls_driver_not_downloader(pool):
    """Rewired steam path delegates to prefill_driver.prefill_apps([app_id]) and
    must NOT touch our chunk downloader (prefill_chunks) or manifest_expand — the
    handler module no longer even imports those steam-downloader symbols."""
    import orchestrator.jobs.handlers.prefill as prefill_mod

    # Regression guard: the steam-downloader path is fully removed from the
    # handler module, so these names must no longer be bound there.
    assert not hasattr(prefill_mod, "prefill_chunks")
    assert not hasattr(prefill_mod, "steam_chunk_download_uri")

    game_id = await _seed_game(pool, app_id="730")

    # The stub steam_client raises if its IPC (manifest_expand/fetch) is touched.
    class _ExplodingSteam(_StubSteam):
        async def manifest_expand(self, raw):
            raise AssertionError("steam path must not call manifest_expand")

        async def manifest_fetch(self, app_id):
            raise AssertionError("steam path must not call manifest_fetch")

    driver = _StubDriver(ok=True)
    deps = Deps(pool=pool, steam_client=_ExplodingSteam(), prefill_driver=driver)
    await prefill_handler(_job(game_id), deps)

    assert driver.calls == [([730], False)]


async def test_steam_force_flag_passthrough(pool):
    game_id = await _seed_game(pool, app_id="730")
    driver = _StubDriver(ok=True)
    await prefill_handler(_job(game_id, force=True), _steam_deps(pool, driver))
    assert driver.calls == [([730], True)]


async def test_steam_success_enqueues_validate_and_marks_cached(pool):
    game_id = await _seed_game(pool, app_id="730")
    await pool.execute_write("UPDATE games SET current_version='42' WHERE id=?", (game_id,))
    driver = _StubDriver(ok=True)
    await prefill_handler(_job(game_id), _steam_deps(pool, driver))

    vj = await pool.read_one(
        "SELECT kind, state FROM jobs WHERE kind='validate' AND game_id=?", (game_id,)
    )
    assert vj == {"kind": "validate", "state": "queued"}
    g = await pool.read_one(
        "SELECT last_prefilled_at, cached_version FROM games WHERE id=?", (game_id,)
    )
    assert g["last_prefilled_at"] is not None
    assert g["cached_version"] == "42"


async def test_steam_failure_marks_game_failed_no_validate(pool):
    game_id = await _seed_game(pool, app_id="730")
    driver = _StubDriver(ok=False)
    with pytest.raises(RuntimeError):
        await prefill_handler(_job(game_id), _steam_deps(pool, driver))
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "failed"
    vj = await pool.read_one("SELECT id FROM jobs WHERE kind='validate' AND game_id=?", (game_id,))
    assert vj is None


async def test_steam_driver_error_marks_failed_not_stuck_downloading(pool):
    """A driver exception (subprocess crash) must resolve to 'failed', not leave
    the game stuck 'downloading' (UAT-10 #2 invariant preserved)."""
    game_id = await _seed_game(pool, app_id="730")

    class _BoomDriver(_StubDriver):
        async def prefill_apps(self, app_ids, *, force=False):
            raise RuntimeError("SteamPrefill subprocess died")

    with pytest.raises(RuntimeError):
        await prefill_handler(_job(game_id), _steam_deps(pool, _BoomDriver()))
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "failed"


async def test_steam_nonnumeric_app_id_raises(pool):
    game_id = await _seed_game(pool, app_id="not-a-number")
    driver = _StubDriver(ok=True)
    with pytest.raises(ValueError):
        await prefill_handler(_job(game_id), _steam_deps(pool, driver))


async def test_unsupported_platform_raises(pool):
    # steam + epic are supported (F6); an unknown platform still rejects.
    game_id = await _seed_game(pool, platform="epic", app_id="fort")
    with pytest.raises(ValueError, match="unsupported platform"):
        await prefill_handler(_job(game_id, "gog"), _steam_deps(pool, _StubDriver()))


async def test_unknown_game_raises(pool):
    with pytest.raises(ValueError, match="not found"):
        await prefill_handler(_job(99999), _steam_deps(pool, _StubDriver()))


async def test_registered():
    from orchestrator.jobs.handlers import HANDLERS, _register_builtin_handlers

    _register_builtin_handlers()
    assert "prefill" in HANDLERS


async def test_summarize_failures_counts_reasons():
    from orchestrator.jobs.handlers.prefill import _summarize_failures

    failures = [
        ("/a", "http 403"),
        ("/b", "http 403"),
        ("/c", "ConnectError"),
        ("/d", "http 403"),
    ]
    assert _summarize_failures(failures) == {"http 403": 3, "ConnectError": 1}


async def test_summarize_failures_keeps_only_top_n():
    from orchestrator.jobs.handlers.prefill import _summarize_failures

    failures = [(f"/{i}", f"reason-{i}") for i in range(10)]  # 10 distinct reasons
    assert len(_summarize_failures(failures, top=3)) == 3
