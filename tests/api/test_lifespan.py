"""Tests for the FastAPI lifespan (spec §4)."""

from __future__ import annotations

import time

import httpx
import pytest

from orchestrator.db.pool import get_pool


class TestLifespanStartup:
    async def test_lifespan_applies_migrations_and_inits_pool(self, lifespan_app):
        pool = get_pool()
        assert pool is not None
        health = await pool.health_check()
        assert health["writer"]["healthy"] is True
        assert health["readers"]["total"] >= 1

    async def test_lifespan_sets_boot_time(self, lifespan_app):
        assert hasattr(lifespan_app.state, "boot_time")
        assert isinstance(lifespan_app.state.boot_time, float)
        assert time.monotonic() - lifespan_app.state.boot_time < 60.0

    async def test_lifespan_sets_git_sha_with_default_unknown(self, lifespan_app):
        assert lifespan_app.state.git_sha == "unknown"

    async def test_lifespan_reads_git_sha_from_env(self, db_path, monkeypatch):
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        monkeypatch.setenv("GIT_SHA", "abc1234")
        app = create_app()
        async with LifespanManager(app):
            assert app.state.git_sha == "abc1234"


class TestLifespanFailures:
    async def test_lifespan_migration_failure_raises_systemexit(self, monkeypatch):
        """V-3 path / dev/null is rejected by migrate.run_migrations →
        the lifespan catches MigrationError and raises SystemExit(1).
        We bypass asgi-lifespan (it swallows SystemExit in its task
        wrapper) and invoke FastAPI's lifespan_context directly."""
        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", "/dev/null")  # V-3 reject path
        app = create_app()
        with pytest.raises(SystemExit):
            async with app.router.lifespan_context(app):
                pass

    async def test_lifespan_returns_503_through_handler_when_unhealthy(self, lifespan_app):
        """Once lifespan is up, a request to /health hits the handler.
        BL5 ship state: 503 because three subsystems are stub-false."""
        transport = httpx.ASGITransport(app=lifespan_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.get("/api/v1/health")
        assert r.status_code == 503  # BL5 ship state per spec §6.4
        body = r.json()
        assert body["status"] in ("ok", "degraded")
        assert body["scheduler_running"] is False  # stub
