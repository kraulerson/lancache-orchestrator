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
        # Wrapped envelope per D2.
        assert isinstance(body, dict)
        assert list(body.keys()) == ["platforms"]
        assert isinstance(body["platforms"], list)

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
