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
