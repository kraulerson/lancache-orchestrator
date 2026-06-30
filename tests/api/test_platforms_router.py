"""Tests for GET /api/v1/platforms (BL6 / Feature 9 partial).

Covers spec §4 — happy path, auth, last_error truncation, config exclusion,
response schema strictness, pool-failure 503 path, ordering + stability.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

VALID_TOKEN = "a" * 32  # matches conftest dummy ORCH_TOKEN


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPlatformsHappyPath:
    async def test_returns_seeded_platforms(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "platforms" in body
        assert len(body["platforms"]) == 2

    async def test_response_envelope_shape(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        # UAT-5 U5-6: envelope extended with `meta` for parity with games/
        # jobs/manifests. Platforms doesn't paginate, so meta just carries
        # `total` + empty applied_filters/applied_sort.
        assert isinstance(body, dict)
        assert set(body.keys()) == {"platforms", "meta"}
        assert isinstance(body["platforms"], list)
        assert set(body["meta"].keys()) == {"total", "applied_filters", "applied_sort"}
        assert body["meta"]["total"] == len(body["platforms"])
        assert body["meta"]["applied_filters"] == {}
        assert body["meta"]["applied_sort"] == []

    # UAT-5 U5-5: cross-router consistency — unknown query params are 400'd
    # everywhere else; platforms was the outlier (silently 200'd).
    async def test_unknown_query_param_returns_400(self, client):
        r = await client.get(
            "/api/v1/platforms?password=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "unknown query parameter" in r.json()["detail"].lower()

    async def test_multiple_unknown_query_params_returns_400(self, client):
        r = await client.get(
            "/api/v1/platforms?foo=1&bar=2",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_response_field_set_per_platform(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for item in body["platforms"]:
            assert set(item.keys()) == {
                "name",
                "auth_status",
                "auth_method",
                "auth_expires_at",
                "last_sync_at",
                "last_error",
            }

    async def test_steam_first_in_order(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["platforms"][0]["name"] == "steam"
        assert body["platforms"][1]["name"] == "epic"


# ---------------------------------------------------------------------------
# Auth (D7)
# ---------------------------------------------------------------------------


class TestPlatformsAuth:
    async def test_no_auth_header_returns_401(self, client):
        r = await client.get("/api/v1/platforms")
        assert r.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    async def test_valid_token_returns_200(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# last_error truncation (D3)
# ---------------------------------------------------------------------------


class TestPlatformsLastErrorTruncation:
    async def _set_last_error(self, populated_pool, name: str, value: str | None) -> None:
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE platforms SET last_error = ? WHERE name = ?",
                (value, name),
            )

    async def test_null_passes_through(self, client, populated_pool):
        await self._set_last_error(populated_pool, "steam", None)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert steam["last_error"] is None

    async def test_under_200_chars_unchanged(self, client, populated_pool):
        s = "x" * 100
        await self._set_last_error(populated_pool, "steam", s)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert steam["last_error"] == s

    async def test_exactly_200_chars_unchanged(self, client, populated_pool):
        s = "x" * 200
        await self._set_last_error(populated_pool, "steam", s)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert steam["last_error"] == s
        assert len(steam["last_error"]) == 200

    async def test_201_chars_truncated_to_200(self, client, populated_pool):
        s = "x" * 201
        await self._set_last_error(populated_pool, "steam", s)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert len(steam["last_error"]) == 200
        assert steam["last_error"] == "x" * 200

    async def test_5000_chars_truncated_to_200(self, client, populated_pool):
        s = "x" * 5000
        await self._set_last_error(populated_pool, "steam", s)
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        steam = next(p for p in r.json()["platforms"] if p["name"] == "steam")
        assert len(steam["last_error"]) == 200


# ---------------------------------------------------------------------------
# config exclusion (D1)
# ---------------------------------------------------------------------------


class TestPlatformsConfigExclusion:
    async def test_config_not_in_response_when_set(self, client, populated_pool):
        sensitive_config = '{"refresh_token": "should-never-appear"}'
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE platforms SET config = ? WHERE name = 'steam'",
                (sensitive_config,),
            )
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        # Config field absent from any item.
        for item in body["platforms"]:
            assert "config" not in item
        # Sensitive value never reaches the wire.
        assert "should-never-appear" not in r.text
        assert "refresh_token" not in r.text

    async def test_config_not_in_response_when_null(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for item in r.json()["platforms"]:
            assert "config" not in item


# ---------------------------------------------------------------------------
# Response schema strictness (D8)
# ---------------------------------------------------------------------------


class TestPlatformsResponseSchema:
    def test_extra_fields_rejected_by_pydantic(self):
        from orchestrator.api.routers.platforms import PlatformResponse

        with pytest.raises(ValidationError):
            PlatformResponse(
                name="steam",
                auth_status="never",
                auth_method="steam_cm",
                auth_expires_at=None,
                last_sync_at=None,
                last_error=None,
                some_unknown_field="should be rejected",  # type: ignore[call-arg]
            )

    def test_invalid_name_rejected_by_literal(self):
        from orchestrator.api.routers.platforms import PlatformResponse

        with pytest.raises(ValidationError):
            PlatformResponse(
                name="origin",  # type: ignore[arg-type]
                auth_status="never",
                auth_method="steam_cm",
                auth_expires_at=None,
                last_sync_at=None,
                last_error=None,
            )

    def test_invalid_auth_status_rejected_by_literal(self):
        from orchestrator.api.routers.platforms import PlatformResponse

        with pytest.raises(ValidationError):
            PlatformResponse(
                name="steam",
                auth_status="bogus",  # type: ignore[arg-type]
                auth_method="steam_cm",
                auth_expires_at=None,
                last_sync_at=None,
                last_error=None,
            )

    def test_invalid_auth_method_rejected_by_literal(self):
        from orchestrator.api.routers.platforms import PlatformResponse

        with pytest.raises(ValidationError):
            PlatformResponse(
                name="steam",
                auth_status="never",
                auth_method="oauth2",  # type: ignore[arg-type]
                auth_expires_at=None,
                last_sync_at=None,
                last_error=None,
            )


# ---------------------------------------------------------------------------
# Pool failure path (D6)
# ---------------------------------------------------------------------------


class TestPlatformsPoolFailure:
    async def test_pool_error_returns_503_with_detail(self, unit_app, client):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _FakeBrokenPool:
            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()

        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}

    async def test_pool_error_logs_structured_event(self, unit_app, client, capsys):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.core.logging import configure_logging
        from orchestrator.db.pool import PoolError

        configure_logging()

        class _FakeBrokenPool:
            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()

        await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        names = [e.get("event") for e in events]
        assert "api.platforms.read_failed" in names
        # Correlation_id propagated through CorrelationIdMiddleware.
        failed = next(e for e in events if e.get("event") == "api.platforms.read_failed")
        assert "correlation_id" in failed


# ---------------------------------------------------------------------------
# Ordering + stability (D4)
# ---------------------------------------------------------------------------


class TestPlatformsOrdering:
    async def test_steam_at_index_0(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["platforms"][0]["name"] == "steam"

    async def test_epic_at_index_1(self, client):
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["platforms"][1]["name"] == "epic"

    async def test_steam_index_distinct_from_epic_index(self, client):
        # Cheap non-rowid-dependent stability check: assert the two
        # platforms end up at distinct indexes 0 and 1 with steam first.
        # The deeper "shake the insert order" test is omitted because
        # FK ON DELETE RESTRICT (games → platforms) makes a true reset
        # impractical from inside the populated_pool fixture.
        r = await client.get(
            "/api/v1/platforms",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        names = [p["name"] for p in r.json()["platforms"]]
        assert names == ["steam", "epic"]


# ---------------------------------------------------------------------------
# Steam auth_status sourced from the live agent/driver signal (the platforms
# column has had NO Steam writer since re-arch ③c — it's orphaned/stale).
# ---------------------------------------------------------------------------


class TestSteamAuthLive:
    class _StubDriver:
        def __init__(self, ok):
            self._ok = ok

        def auth_status(self):
            from types import SimpleNamespace

            return SimpleNamespace(ok=self._ok)

    def _co_located(self, monkeypatch):
        from orchestrator.core.settings import Settings

        monkeypatch.setattr(
            "orchestrator.api.routers.platforms.get_settings",
            lambda: Settings(orchestrator_token="a" * 32, agent_enabled=False),
        )

    async def _steam_row(self, unit_app):
        import httpx

        transport = httpx.ASGITransport(app=unit_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.get("/api/v1/platforms", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
        assert r.status_code == 200
        return next(p for p in r.json()["platforms"] if p["name"] == "steam")

    async def test_live_driver_ok_overrides_stale_expired_column(
        self, unit_app, populated_pool, monkeypatch
    ):
        self._co_located(monkeypatch)
        await populated_pool.execute_write(
            "UPDATE platforms SET auth_status='expired', last_error='stale' WHERE name='steam'"
        )
        unit_app.state.prefill_driver = self._StubDriver(ok=True)
        steam = await self._steam_row(unit_app)
        assert steam["auth_status"] == "ok"
        assert steam["last_error"] is None  # stale last_error cleared on override

    async def test_live_driver_not_ok_reports_expired(self, unit_app, populated_pool, monkeypatch):
        self._co_located(monkeypatch)
        await populated_pool.execute_write(
            "UPDATE platforms SET auth_status='ok' WHERE name='steam'"
        )
        unit_app.state.prefill_driver = self._StubDriver(ok=False)
        steam = await self._steam_row(unit_app)
        assert steam["auth_status"] == "expired"

    async def test_no_live_signal_falls_back_to_db(self, unit_app, populated_pool, monkeypatch):
        # agent_enabled but no agent_client (or no driver) -> indeterminate -> DB value.
        self._co_located(monkeypatch)  # co-located, but no prefill_driver on app.state
        await populated_pool.execute_write(
            "UPDATE platforms SET auth_status='expired' WHERE name='steam'"
        )
        steam = await self._steam_row(unit_app)
        assert steam["auth_status"] == "expired"  # unchanged (defensive fallback)

    async def test_live_check_error_falls_back_to_db(self, unit_app, populated_pool, monkeypatch):
        self._co_located(monkeypatch)

        class _BoomDriver:
            def auth_status(self):
                raise RuntimeError("driver boom")

        await populated_pool.execute_write(
            "UPDATE platforms SET auth_status='expired' WHERE name='steam'"
        )
        unit_app.state.prefill_driver = _BoomDriver()
        steam = await self._steam_row(unit_app)
        assert steam["auth_status"] == "expired"  # exception -> None -> DB fallback

    async def test_get_settings_error_falls_back_to_db(self, unit_app, populated_pool, monkeypatch):
        # The docstring promises "Never raises". A get_settings() failure (e.g. a
        # future runtime reload with invalid config) must fall back to the stored
        # column value, NOT 500 the status page (UAT-13 F1 / #210).
        def _boom():
            raise RuntimeError("settings boom")

        monkeypatch.setattr("orchestrator.api.routers.platforms.get_settings", _boom)
        await populated_pool.execute_write(
            "UPDATE platforms SET auth_status='ok' WHERE name='steam'"
        )
        steam = await self._steam_row(unit_app)  # asserts HTTP 200 internally
        assert steam["auth_status"] == "ok"  # get_settings() raised -> None -> DB fallback

    async def test_epic_auth_status_unaffected(self, unit_app, populated_pool, monkeypatch):
        self._co_located(monkeypatch)
        await populated_pool.execute_write(
            "UPDATE platforms SET auth_status='ok' WHERE name='epic'"
        )
        unit_app.state.prefill_driver = self._StubDriver(ok=False)  # would flip steam expired
        import httpx

        transport = httpx.ASGITransport(app=unit_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.get("/api/v1/platforms", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
        epic = next(p for p in r.json()["platforms"] if p["name"] == "epic")
        assert epic["auth_status"] == "ok"  # epic read from DB, never overridden

    async def test_live_agent_signal_overrides_when_agent_enabled(
        self, unit_app, populated_pool, monkeypatch
    ):
        from orchestrator.core.settings import Settings

        monkeypatch.setattr(
            "orchestrator.api.routers.platforms.get_settings",
            lambda: Settings(orchestrator_token="a" * 32, agent_enabled=True),
        )

        class _StubAgent:
            async def auth_status(self):
                return {"ok": True}

        await populated_pool.execute_write(
            "UPDATE platforms SET auth_status='expired' WHERE name='steam'"
        )
        unit_app.state.agent_client = _StubAgent()
        steam = await self._steam_row(unit_app)
        assert steam["auth_status"] == "ok"
