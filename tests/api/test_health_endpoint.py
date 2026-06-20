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

    async def test_remaining_stubbed_subsystems_are_false(self, client):
        """scheduler_running + validator_healthy stay stubbed until those
        subsystems ship. lancache_reachable is now wired to the probe
        (ID2); the unit_app fixture omits app.state.lancache_probe so it
        also reports False — exercised by the next test."""
        r = await client.get("/api/v1/health")
        body = r.json()
        assert body["scheduler_running"] is False
        assert body["validator_healthy"] is False

    async def test_lancache_reachable_false_when_probe_absent(self, client):
        """The unit_app fixture skips lifespan, so app.state.lancache_probe
        is not set. /health must fall back to False rather than crashing."""
        r = await client.get("/api/v1/health")
        assert r.status_code == 503
        body = r.json()
        assert body["lancache_reachable"] is False

    async def test_lancache_reachable_true_when_probe_reports_up(self, client):
        """Inject a stub probe that always returns True; verify /health
        surfaces the value."""

        class _StubProbe:
            async def probe(self):
                return True

        client._transport.app.state.lancache_probe = _StubProbe()
        try:
            r = await client.get("/api/v1/health")
            body = r.json()
            assert body["lancache_reachable"] is True
        finally:
            del client._transport.app.state.lancache_probe

    async def test_lancache_reachable_false_when_probe_reports_down(self, client):
        class _StubProbe:
            async def probe(self):
                return False

        client._transport.app.state.lancache_probe = _StubProbe()
        try:
            r = await client.get("/api/v1/health")
            body = r.json()
            assert body["lancache_reachable"] is False
        finally:
            del client._transport.app.state.lancache_probe

    async def test_scheduler_running_false_when_manager_absent(self, client):
        """No-lifespan unit_app omits app.state.scheduler_manager.
        /health must fall back to False rather than crashing."""
        r = await client.get("/api/v1/health")
        body = r.json()
        assert body["scheduler_running"] is False

    async def test_scheduler_running_true_when_manager_reports_running(self, client):
        class _StubManager:
            running = True

        client._transport.app.state.scheduler_manager = _StubManager()
        try:
            r = await client.get("/api/v1/health")
            body = r.json()
            assert body["scheduler_running"] is True
        finally:
            del client._transport.app.state.scheduler_manager

    async def test_scheduler_running_false_when_manager_reports_stopped(self, client):
        class _StubManager:
            running = False

        client._transport.app.state.scheduler_manager = _StubManager()
        try:
            r = await client.get("/api/v1/health")
            body = r.json()
            assert body["scheduler_running"] is False
        finally:
            del client._transport.app.state.scheduler_manager


class TestValidatorHealth:
    """F7: validator_healthy reflects app.state and gates the 200/503 result."""

    async def test_validator_healthy_reflects_app_state_true(self, client):
        client._transport.app.state.validator_healthy = True
        try:
            r = await client.get("/api/v1/health")
            assert r.json()["validator_healthy"] is True
        finally:
            del client._transport.app.state.validator_healthy

    async def test_all_healthy_returns_200_only_with_validator(self, client, monkeypatch, tmp_path):
        """With every other subsystem healthy, validator_healthy is the
        deciding term: True -> 200, False -> 503."""

        class _StubProbe:
            async def probe(self):
                return True

        class _StubManager:
            running = True

        # cache_volume_mounted needs a real dir.
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(cache_dir))
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()

        app_state = client._transport.app.state
        app_state.lancache_probe = _StubProbe()
        app_state.scheduler_manager = _StubManager()
        try:
            app_state.validator_healthy = True
            r_ok = await client.get("/api/v1/health")
            assert r_ok.status_code == 200

            app_state.validator_healthy = False
            r_bad = await client.get("/api/v1/health")
            assert r_bad.status_code == 503
            assert r_bad.json()["validator_healthy"] is False
        finally:
            del app_state.lancache_probe
            del app_state.scheduler_manager
            del app_state.validator_healthy
            get_settings.cache_clear()


class TestSteamAuthStatus:
    """Steam auth status on /health reflects prefill_driver.auth_status()."""

    async def test_steam_auth_ok_field_present(self, client):
        r = await client.get("/api/v1/health")
        assert "steam_auth_ok" in r.json()

    async def test_steam_auth_ok_false_when_driver_absent(self, client):
        """No-lifespan unit_app omits app.state.prefill_driver — fall back to
        False rather than crashing (BL5-stub-like)."""
        r = await client.get("/api/v1/health")
        assert r.json()["steam_auth_ok"] is False

    async def test_steam_auth_ok_true_when_driver_reports_ok(self, client):
        from orchestrator.platform.steam.prefill_driver import SteamAuthStatus

        class _StubDriver:
            def auth_status(self):
                return SteamAuthStatus(ok=True)

        client._transport.app.state.prefill_driver = _StubDriver()
        try:
            r = await client.get("/api/v1/health")
            assert r.json()["steam_auth_ok"] is True
        finally:
            del client._transport.app.state.prefill_driver

    async def test_steam_auth_ok_false_when_driver_needs_reauth(self, client):
        from orchestrator.platform.steam.prefill_driver import SteamAuthStatus

        class _StubDriver:
            def auth_status(self):
                return SteamAuthStatus(ok=False, reason="no_account_config")

        client._transport.app.state.prefill_driver = _StubDriver()
        try:
            r = await client.get("/api/v1/health")
            assert r.json()["steam_auth_ok"] is False
        finally:
            del client._transport.app.state.prefill_driver


class TestHealthDynamicFields:
    async def test_uptime_sec_increases_monotonically(self, client):
        r1 = await client.get("/api/v1/health")
        await asyncio.sleep(1.1)
        r2 = await client.get("/api/v1/health")
        assert r2.json()["uptime_sec"] >= r1.json()["uptime_sec"] + 1

    async def test_git_sha_echoes_app_state_truncated(self, client, unit_app):
        # UAT-3 S2-B: /health is unauthenticated, so the git_sha is
        # truncated to 8 chars before being returned to the client.
        r = await client.get("/api/v1/health")
        assert r.json()["git_sha"] == unit_app.state.git_sha[:8]

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
