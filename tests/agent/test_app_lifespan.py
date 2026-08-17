"""NEW-1 (review 2026-06-23): the agent app had no lifespan shutdown, so on a
redeploy the dedicated cache-stat thread pool was leaked and in-flight
fire-and-forget tasks (prefill/pull) were abandoned. These tests assert the
shutdown tears both down."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.agent import app as agent_app_mod
from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings

pytestmark = pytest.mark.asyncio

TOKEN = "a" * 32


async def test_lifespan_shutdown_cancels_pending_bg_tasks():
    app = create_agent_app(settings=Settings(orchestrator_token=TOKEN))
    async with app.router.lifespan_context(app):

        async def _forever() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        app.state.agent_bg_tasks.add(task)
        await asyncio.sleep(0)  # let it start
        assert not task.done()
    # After lifespan exit the abandoned task must be cancelled, not leaked.
    assert task.cancelled()


async def test_lifespan_shutdown_tears_down_cache_stat_executor(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        agent_app_mod,
        "shutdown_cache_stat_executor",
        lambda: calls.__setitem__("n", calls["n"] + 1),
    )
    app = create_agent_app(settings=Settings(orchestrator_token=TOKEN))
    async with app.router.lifespan_context(app):
        pass
    assert calls["n"] == 1


# --- Durable manifest archive: periodic sync task wiring ---


async def test_sync_task_wired_when_enabled():
    app = create_agent_app(
        settings=Settings(orchestrator_token=TOKEN, manifest_archive_sync_interval_sec=1800)
    )
    async with app.router.lifespan_context(app):
        assert len(app.state.agent_bg_tasks) == 1  # the sync loop task


async def test_sync_task_absent_when_disabled():
    app = create_agent_app(
        settings=Settings(orchestrator_token=TOKEN, manifest_archive_sync_interval_sec=0)
    )
    async with app.router.lifespan_context(app):
        assert len(app.state.agent_bg_tasks) == 0


async def test_loop_runs_immediately(monkeypatch):
    import contextlib
    from pathlib import Path

    import orchestrator.agent.manifest_archive as marc

    calls = []
    monkeypatch.setattr(marc, "sync_manifests_to_archive", lambda *a, **k: calls.append(1) or 0)
    task = asyncio.create_task(marc.manifest_archive_sync_loop(Path("/live"), Path("/arch"), 3600))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert calls  # ran once immediately, before the first sleep


async def test_sync_task_uses_the_live_prefill_cache_as_source(monkeypatch):
    """The sync loop must read the dir SteamPrefill actually writes to.

    Found live 2026-08-17: the lifespan started the loop with
    `steam_manifest_cache_dir` (/steamprefill-cache) as live_root. That is one of
    the roots `prefilled-apps` already enumerates, and it is static — so every
    cycle copied 0 files and, because the success log is guarded by `if copied:`,
    it did so in total silence for a week.

    Meanwhile the host prefill cron runs SteamPrefill with HOME=/tmp, so real
    manifests land in `steam_prefill_live_cache_dir` (/tmp/.cache/SteamPrefill) —
    a dir nothing synced and which is lost on container restart. 11 newly
    purchased games were fully downloaded into lancache yet stayed invisible to
    the orchestrator because their manifests never reached an enumerated root.
    """
    captured: dict[str, object] = {}

    def _fake_loop(live_root, archive_root, interval_sec, **kw):
        captured["live_root"] = live_root
        captured["archive_root"] = archive_root

        async def _noop() -> None:
            await asyncio.sleep(3600)

        return _noop()

    monkeypatch.setattr(agent_app_mod, "manifest_archive_sync_loop", _fake_loop)
    settings = Settings(orchestrator_token=TOKEN, manifest_archive_sync_interval_sec=1800)
    app = create_agent_app(settings=settings)
    async with app.router.lifespan_context(app):
        pass

    assert captured["live_root"] == settings.steam_prefill_live_cache_dir
    assert captured["archive_root"] == settings.steam_manifest_archive_dir


async def test_steam_prefill_timeout_setting_actually_reaches_the_driver():
    """ORCH_STEAM_PREFILL_TIMEOUT_SEC must not be inert.

    `create_agent_app` attaches `prefill_driver` EAGERLY (so the POST and GET
    share one instance), and the lifespan then guards its own construction with
    `if not hasattr(app.state, "prefill_driver")` — which is therefore always
    False. Passing timeout_sec only in the lifespan branch means the eager
    driver, the one every request actually uses, silently keeps the 36000s
    default. An operator lowering the timeout would get no effect and no
    warning; the lifespan's hunk is dead code.

    Asserted both before and after the lifespan runs, because the eager
    instance must not be replaced either.
    """
    settings = Settings(orchestrator_token=TOKEN, steam_prefill_timeout_sec=12.0)
    app = create_agent_app(settings=settings)

    assert app.state.prefill_driver._timeout_sec == 12.0, "eager driver ignored the setting"
    eager = app.state.prefill_driver
    async with app.router.lifespan_context(app):
        assert app.state.prefill_driver is eager, "lifespan replaced the shared driver"
        assert app.state.prefill_driver._timeout_sec == 12.0
