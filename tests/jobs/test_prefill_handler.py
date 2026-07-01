"""Tests for orchestrator.jobs.handlers.prefill (F5)."""

from __future__ import annotations

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.handlers.prefill import prefill_handler
from orchestrator.jobs.worker import Deps
from orchestrator.platform.steam.prefill_driver import PrefillResult as DriverPrefillResult

pytestmark = pytest.mark.asyncio

SHA_A = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"
SHA_B = "234a47ed3005727db220987ecac460030295bd79"


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
    """Deps for the rewired steam path: prefill goes through the driver."""
    return Deps(pool=pool, prefill_driver=driver)


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

    driver = _StubDriver(ok=True)
    deps = Deps(pool=pool, prefill_driver=driver)
    await prefill_handler(_job(game_id), deps)

    assert driver.calls == [([730], False)]


async def test_steam_prefill_ignores_dead_force_key(pool):
    """CORE-2 (review 2026-06-23): `force` was read from a job key the job row
    never carries (always False in prod) — a misleading dead flag. The handler
    no longer threads a per-job force; even a stray 'force' key is ignored and the
    driver is called with its default (False)."""
    game_id = await _seed_game(pool, app_id="730")
    driver = _StubDriver(ok=True)
    await prefill_handler(_job(game_id, force=True), _steam_deps(pool, driver))
    assert driver.calls == [([730], False)]


async def test_steam_prefill_payload_force_threads_force(pool):
    """Force-prefill: a job whose payload carries {"force": true} threads
    force=True into the driver. This is the live re-introduction of a per-job
    force — sourced from the job payload (which the worker selects), NOT the
    top-level dead key the CORE-2 cleanup removed."""
    game_id = await _seed_game(pool, app_id="730")
    driver = _StubDriver(ok=True)
    await prefill_handler(_job(game_id, payload='{"force": true}'), _steam_deps(pool, driver))
    assert driver.calls == [([730], True)]


async def test_steam_prefill_payload_without_force_is_false(pool):
    """A payload that omits force (e.g. {}) leaves force at its default False."""
    game_id = await _seed_game(pool, app_id="730")
    driver = _StubDriver(ok=True)
    await prefill_handler(_job(game_id, payload="{}"), _steam_deps(pool, driver))
    assert driver.calls == [([730], False)]


async def test_steam_prefill_malformed_payload_is_false(pool):
    """A non-JSON / unexpected payload must not crash the handler — force=False."""
    game_id = await _seed_game(pool, app_id="730")
    driver = _StubDriver(ok=True)
    await prefill_handler(_job(game_id, payload="not-json"), _steam_deps(pool, driver))
    assert driver.calls == [([730], False)]


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


# --- DPA-T10: agent control-plane seam (settings.agent_enabled=True) ---


class _FakeAgent:
    """Stand-in for AgentClient — records steam_prefill calls."""

    def __init__(self, *, ok=True, raw="ok"):
        self._ok = ok
        self._raw = raw
        self.calls: list[tuple[list[int], bool]] = []

    async def steam_prefill(self, app_ids, *, force=False):
        self.calls.append((list(app_ids), force))
        return {"ok": self._ok, "raw": self._raw}


class _ExplodingDriver(_StubDriver):
    """Proves the driver path is NOT taken when the agent flag is on."""

    async def prefill_apps(self, app_ids, *, force=False):
        raise AssertionError("agent_enabled path must not call prefill_driver")


def _agent_enabled(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.jobs.handlers.prefill.get_settings",
        lambda: Settings(orchestrator_token="a" * 32, agent_enabled=True),
    )


async def test_steam_agent_path_taken_not_driver(pool, monkeypatch):
    """With agent_enabled=True the steam prefill routes through agent_client and
    the in-process driver is never touched."""
    _agent_enabled(monkeypatch)
    game_id = await _seed_game(pool, app_id="730")
    agent = _FakeAgent(ok=True)
    deps = Deps(
        pool=pool,
        prefill_driver=_ExplodingDriver(),
        agent_client=agent,
    )
    await prefill_handler(_job(game_id), deps)
    assert agent.calls == [([730], False)]


async def test_steam_agent_prefill_ignores_dead_force_key(pool, monkeypatch):
    """CORE-2: the agent seam likewise no longer threads a per-job force."""
    _agent_enabled(monkeypatch)
    game_id = await _seed_game(pool, app_id="730")
    agent = _FakeAgent(ok=True)
    deps = Deps(
        pool=pool,
        prefill_driver=_ExplodingDriver(),
        agent_client=agent,
    )
    await prefill_handler(_job(game_id, force=True), deps)
    assert agent.calls == [([730], False)]


async def test_steam_agent_payload_force_threads_force(pool, monkeypatch):
    """Force-prefill, agent seam: payload {"force": true} threads force=True to
    agent_client.steam_prefill (matching the in-process driver path)."""
    _agent_enabled(monkeypatch)
    game_id = await _seed_game(pool, app_id="730")
    agent = _FakeAgent(ok=True)
    deps = Deps(pool=pool, prefill_driver=_ExplodingDriver(), agent_client=agent)
    await prefill_handler(_job(game_id, payload='{"force": true}'), deps)
    assert agent.calls == [([730], True)]


async def test_steam_agent_success_same_db_writes(pool, monkeypatch):
    """The agent happy path produces the SAME DB writes as the flag-off path:
    validate enqueued + last_prefilled_at + cached_version=current_version."""
    _agent_enabled(monkeypatch)
    game_id = await _seed_game(pool, app_id="730")
    await pool.execute_write("UPDATE games SET current_version='42' WHERE id=?", (game_id,))
    agent = _FakeAgent(ok=True)
    deps = Deps(
        pool=pool,
        prefill_driver=_ExplodingDriver(),
        agent_client=agent,
    )
    await prefill_handler(_job(game_id), deps)

    vj = await pool.read_one(
        "SELECT kind, state FROM jobs WHERE kind='validate' AND game_id=?", (game_id,)
    )
    assert vj == {"kind": "validate", "state": "queued"}
    g = await pool.read_one(
        "SELECT last_prefilled_at, cached_version FROM games WHERE id=?", (game_id,)
    )
    assert g["last_prefilled_at"] is not None
    assert g["cached_version"] == "42"


async def test_steam_agent_failure_marks_failed_no_validate(pool, monkeypatch):
    """agent returns ok=False → game marked 'failed' with last_error, RuntimeError
    raised, and no validate job enqueued (mirrors the driver-non-ok path)."""
    _agent_enabled(monkeypatch)
    game_id = await _seed_game(pool, app_id="730")
    agent = _FakeAgent(ok=False, raw="boom: exited 1")
    deps = Deps(
        pool=pool,
        prefill_driver=_ExplodingDriver(),
        agent_client=agent,
    )
    with pytest.raises(RuntimeError):
        await prefill_handler(_job(game_id), deps)
    g = await pool.read_one("SELECT status, last_error FROM games WHERE id=?", (game_id,))
    assert g["status"] == "failed"
    assert g["last_error"] is not None
    vj = await pool.read_one("SELECT id FROM jobs WHERE kind='validate' AND game_id=?", (game_id,))
    assert vj is None


async def test_steam_agent_enabled_but_no_agent_client_raises(pool, monkeypatch):
    """agent_enabled=True but no agent_client wired → explicit RuntimeError."""
    _agent_enabled(monkeypatch)
    game_id = await _seed_game(pool, app_id="730")
    deps = Deps(pool=pool, prefill_driver=_StubDriver())
    with pytest.raises(RuntimeError, match="agent_client"):
        await prefill_handler(_job(game_id), deps)


# --- DPA-T11: Epic prefill seam (settings.agent_enabled=True) ---


class _StubEpic:
    """Stand-in for EpicClient — fetch_manifest returns (manifest, host, base)."""

    def __init__(self, manifest):
        self._manifest = manifest

    async def fetch_manifest(self, item):
        return self._manifest, "epiccdn.test", "/base"


class _FakeEpicAgent:
    """Stand-in for AgentClient — records pull() calls (Epic bulk download)."""

    def __init__(self, *, chunks_failed=0):
        self._chunks_failed = chunks_failed
        self.calls: list[dict] = []

    async def pull(self, chunks, *, user_agent, concurrency=None):
        n = len(chunks)
        self.calls.append(
            {"chunks": list(chunks), "user_agent": user_agent, "concurrency": concurrency}
        )
        failed = self._chunks_failed
        return {
            "chunks_total": n,
            "chunks_ok": n - failed,
            "chunks_failed": failed,
            "failures": [("/x", "http 403")] * failed,
        }


def _epic_manifest():
    from orchestrator.platform.epic.models import EpicChunk, EpicManifest

    chunks = [EpicChunk((1, 2, 3, 4), 100, b"x" * 20, 0, 500, 1048576)]
    return EpicManifest(version=22, chunks=chunks, cdn_base="/base", raw=b"BINARY-MANIFEST")


def _expected_epic_specs(manifest):
    """The dedup'd chunk specs the handler should hand the agent."""
    from orchestrator.platform.epic.manifest import chunk_path as epic_chunk_path
    from orchestrator.prefill.epic_downloader import _full_path

    seen: set[str] = set()
    specs: list[dict[str, str]] = []
    for chunk in manifest.chunks:
        p = epic_chunk_path(chunk, manifest.version)
        if p not in seen:
            seen.add(p)
            specs.append({"url": _full_path("/base", p), "host": "epiccdn.test"})
    return specs


async def _seed_epic_game(pool, *, app_id="AppA"):
    import json

    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, metadata) VALUES "
        "('epic', ?, 't', 1, ?)",
        (app_id, json.dumps({"namespace": "ns", "catalog_item_id": "cat"})),
    )
    row = await pool.read_one("SELECT id FROM games WHERE platform='epic' AND app_id=?", (app_id,))
    return row["id"]


async def test_epic_agent_path_pulls_through_agent_not_downloader(pool, monkeypatch):
    """With agent_enabled=True the Epic bulk download routes through
    agent_client.pull() with the correct chunk specs + user_agent, and the
    in-process epic_prefill_chunks is never touched. epic_verify_cached STAYS
    control-side (deferred from the agent)."""
    import orchestrator.jobs.handlers.prefill as ph

    settings = Settings(orchestrator_token="a" * 32, agent_enabled=True)
    monkeypatch.setattr(ph, "get_settings", lambda: settings)

    async def _explode_prefill(*a, **kw):
        raise AssertionError("agent_enabled Epic path must not call epic_prefill_chunks")

    verify_calls: list[tuple] = []

    async def _fake_verify(paths, host, base, settings, **kw):
        verify_calls.append((tuple(paths), host, base))
        return 1.0

    monkeypatch.setattr(ph, "epic_prefill_chunks", _explode_prefill)
    monkeypatch.setattr(ph, "epic_verify_cached", _fake_verify)

    manifest = _epic_manifest()
    gid = await _seed_epic_game(pool, app_id="AppPull")
    agent = _FakeEpicAgent()
    deps = Deps(pool=pool, epic_client=_StubEpic(manifest), agent_client=agent)
    await prefill_handler(_job(gid, platform="epic"), deps)

    # (1) agent.pull called with the expected specs + epic UA.
    assert len(agent.calls) == 1
    assert agent.calls[0]["chunks"] == _expected_epic_specs(manifest)
    assert agent.calls[0]["user_agent"] == settings.epic_user_agent
    # (2) verify_cached WAS still called control-side (first 20 of the same paths).
    assert len(verify_calls) == 1


async def test_epic_agent_success_same_db_writes(pool, monkeypatch):
    """The Epic agent happy path produces the SAME final DB writes as the flag-off
    Epic path: status up_to_date, cached_version=current_version, last_prefilled_at,
    and the manifest upsert + size_bytes."""
    import orchestrator.jobs.handlers.prefill as ph

    settings = Settings(orchestrator_token="a" * 32, agent_enabled=True)
    monkeypatch.setattr(ph, "get_settings", lambda: settings)

    async def _fake_verify(paths, host, base, settings, **kw):
        return 1.0

    monkeypatch.setattr(ph, "epic_verify_cached", _fake_verify)

    gid = await _seed_epic_game(pool, app_id="AppOK")
    await pool.execute_write("UPDATE games SET current_version='bv-1' WHERE id=?", (gid,))
    deps = Deps(
        pool=pool,
        epic_client=_StubEpic(_epic_manifest()),
        agent_client=_FakeEpicAgent(),
    )
    await prefill_handler(_job(gid, platform="epic"), deps)

    g = await pool.read_one(
        "SELECT status, size_bytes, cached_version, last_prefilled_at FROM games WHERE id=?", (gid,)
    )
    assert g["status"] == "up_to_date"
    assert g["size_bytes"] == 500
    assert g["cached_version"] == "bv-1"
    assert g["last_prefilled_at"] is not None
    m = await pool.read_one(
        "SELECT chunk_count, total_bytes FROM manifests WHERE game_id=?", (gid,)
    )
    assert m["chunk_count"] == 1
    assert m["total_bytes"] == 500


async def test_epic_agent_failed_chunks_marks_failed(pool, monkeypatch):
    """agent.pull reporting chunks_failed>0 → game 'failed' with last_error and a
    RuntimeError raised (mirrors the in-process chunks-failed path)."""
    import orchestrator.jobs.handlers.prefill as ph

    settings = Settings(orchestrator_token="a" * 32, agent_enabled=True)
    monkeypatch.setattr(ph, "get_settings", lambda: settings)
    monkeypatch.setattr(ph, "epic_verify_cached", _unreachable_verify := _make_unreachable_verify())

    gid = await _seed_epic_game(pool, app_id="AppFail")
    deps = Deps(
        pool=pool,
        epic_client=_StubEpic(_epic_manifest()),
        agent_client=_FakeEpicAgent(chunks_failed=1),
    )
    with pytest.raises(RuntimeError):
        await prefill_handler(_job(gid, platform="epic"), deps)
    g = await pool.read_one("SELECT status, last_error FROM games WHERE id=?", (gid,))
    assert g["status"] == "failed"
    assert g["last_error"] is not None
    assert _unreachable_verify.called is False  # verify is skipped on the failure path


def _make_unreachable_verify():
    async def _verify(paths, host, base, settings, **kw):
        _verify.called = True
        return 1.0

    _verify.called = False
    return _verify


async def test_epic_manifest_stores_cdn_base(pool, monkeypatch):
    """Epic prefill must persist manifest.cdn_base in the manifests row so the
    Epic validator can compute the lancache cache-key without re-fetching a signed
    manifest. Before the fix the INSERT omits cdn_base and the row is NULL."""
    import orchestrator.jobs.handlers.prefill as ph

    settings = Settings(orchestrator_token="a" * 32, agent_enabled=True)
    monkeypatch.setattr(ph, "get_settings", lambda: settings)

    async def _fake_verify(paths, host, base, settings, **kw):
        return 1.0

    monkeypatch.setattr(ph, "epic_verify_cached", _fake_verify)

    manifest = _epic_manifest()  # cdn_base="/base"
    gid = await _seed_epic_game(pool, app_id="AppCdnBase")
    deps = Deps(
        pool=pool,
        epic_client=_StubEpic(manifest),
        agent_client=_FakeEpicAgent(),
    )
    await prefill_handler(_job(gid, platform="epic"), deps)

    m = await pool.read_one("SELECT cdn_base FROM manifests WHERE game_id=?", (gid,))
    assert m is not None
    assert m["cdn_base"] == manifest.cdn_base


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
