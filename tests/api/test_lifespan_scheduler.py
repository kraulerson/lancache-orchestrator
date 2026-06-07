"""Integration test for F12: the scheduler runs in FastAPI lifespan
startup, exposes `running=True` on `app.state.scheduler_manager`, and
cleanly shuts down when the lifespan exits."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestLifespanSchedulerIntegration:
    async def test_scheduler_starts_and_running_on_boot(self, db_path, monkeypatch):
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        # Use a fast cycle so we never accidentally fire during the test.
        monkeypatch.setenv("ORCH_SCHEDULER_LIBRARY_SYNC_INTERVAL_SEC", "86400")

        app = create_app()
        async with LifespanManager(app):
            mgr = app.state.scheduler_manager
            assert mgr is not None
            assert mgr.running is True
            assert "library_sync_steam" in mgr.get_registered_job_ids()
        # After exiting lifespan, scheduler is stopped.
        assert app.state.scheduler_manager.running is False

    async def test_scheduler_disabled_via_settings(self, db_path, monkeypatch):
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        monkeypatch.setenv("ORCH_SCHEDULER_ENABLED", "false")

        app = create_app()
        async with LifespanManager(app):
            mgr = app.state.scheduler_manager
            assert mgr is not None
            assert mgr.running is False
            assert mgr.get_registered_job_ids() == []

    async def test_validation_sweep_job_registered_on_boot(self, db_path, monkeypatch):
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app
        from orchestrator.scheduler.manager import VALIDATION_SWEEP_JOB_ID

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        monkeypatch.setenv("ORCH_SCHEDULER_LIBRARY_SYNC_INTERVAL_SEC", "86400")

        app = create_app()
        async with LifespanManager(app):
            mgr = app.state.scheduler_manager
            assert VALIDATION_SWEEP_JOB_ID in mgr.get_registered_job_ids()

    async def test_validation_sweep_disabled_via_settings(self, db_path, monkeypatch):
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app
        from orchestrator.scheduler.manager import VALIDATION_SWEEP_JOB_ID

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        monkeypatch.setenv("ORCH_SCHEDULER_LIBRARY_SYNC_INTERVAL_SEC", "86400")
        monkeypatch.setenv("ORCH_VALIDATION_SWEEP_ENABLED", "false")

        app = create_app()
        async with LifespanManager(app):
            ids = app.state.scheduler_manager.get_registered_job_ids()
            assert VALIDATION_SWEEP_JOB_ID not in ids
            assert "library_sync_steam" in ids  # scheduler itself still runs

    async def test_health_scheduler_running_true_under_lifespan(self, db_path, monkeypatch):
        """End-to-end: /health surfaces scheduler_running=True when the
        scheduler is up in lifespan."""
        import httpx
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        monkeypatch.setenv("ORCH_SCHEDULER_LIBRARY_SYNC_INTERVAL_SEC", "86400")

        app = create_app()
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r = await client.get("/api/v1/health")
                body = r.json()
                assert body["scheduler_running"] is True
