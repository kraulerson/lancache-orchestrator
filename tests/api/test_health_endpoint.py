"""Tests for /api/v1/health endpoint (spec §6)."""

from __future__ import annotations

import asyncio

import pytest


class TestHealthShipState:
    async def test_bl5_ship_state_returns_503(self, client):
        """Per spec §6.4: scheduler/lancache/validator are stubbed false
        in BL5 → /health must return 503."""
        r = await client.get("/api/v1/health")
        assert r.status_code == 503

    async def test_response_has_all_required_fields(self, client):
        r = await client.get("/api/v1/health")
        body = r.json()
        for field in [
            "status",
            "version",
            "uptime_sec",
            "scheduler_running",
            "lancache_reachable",
            "cache_volume_mounted",
            "validator_healthy",
            "git_sha",
        ]:
            assert field in body, f"missing field: {field}"

    async def test_status_is_ok_when_pool_healthy(self, client):
        r = await client.get("/api/v1/health")
        body = r.json()
        # populated_pool fixture has healthy pool → status="ok"
        assert body["status"] == "ok"

    async def test_three_stubbed_subsystems_are_false_in_bl5(self, client):
        r = await client.get("/api/v1/health")
        body = r.json()
        assert body["scheduler_running"] is False
        assert body["lancache_reachable"] is False
        assert body["validator_healthy"] is False


class TestHealthDynamicFields:
    async def test_uptime_sec_increases_monotonically(self, client):
        r1 = await client.get("/api/v1/health")
        await asyncio.sleep(1.1)
        r2 = await client.get("/api/v1/health")
        assert r2.json()["uptime_sec"] >= r1.json()["uptime_sec"] + 1

    async def test_git_sha_echoes_app_state(self, client, unit_app):
        r = await client.get("/api/v1/health")
        assert r.json()["git_sha"] == unit_app.state.git_sha

    async def test_cache_volume_mounted_reflects_stat(self, client, monkeypatch, tmp_path):
        from orchestrator.core.settings import get_settings

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(cache_dir))
        get_settings.cache_clear()
        r = await client.get("/api/v1/health")
        assert r.json()["cache_volume_mounted"] is True


class TestHealthDegradedTransitions:
    async def test_pool_unhealthy_drops_status_to_degraded(
        self, client, monkeypatch, populated_pool
    ):
        original = populated_pool.health_check

        async def fake_health():
            result = await original()
            result["writer"]["healthy"] = False
            return result

        monkeypatch.setattr(populated_pool, "health_check", fake_health)
        r = await client.get("/api/v1/health")
        assert r.status_code == 503
        assert r.json()["status"] == "degraded"


class TestHealthResponseShape:
    async def test_content_type_application_json(self, client):
        r = await client.get("/api/v1/health")
        assert r.headers["content-type"].startswith("application/json")

    async def test_response_model_extra_forbid(self):
        """Spec §6.1: HealthResponse has extra='forbid'."""
        from pydantic import ValidationError

        from orchestrator.api.routers.health import HealthResponse

        with pytest.raises(ValidationError):
            HealthResponse(
                status="ok",
                version="x",
                uptime_sec=0,
                scheduler_running=False,
                lancache_reachable=False,
                cache_volume_mounted=False,
                validator_healthy=False,
                git_sha="x",
                unknown_extra_field="leak",  # forbidden
            )
