"""Tests for BearerAuthMiddleware (spec §5.4)."""

from __future__ import annotations

import json

VALID_TOKEN = "a" * 32  # matches the conftest dummy token


class TestBearerAuthExempt:
    async def test_health_path_no_auth_required(self, client):
        r = await client.get("/api/v1/health")
        assert r.status_code != 401

    async def test_openapi_json_no_auth_required(self, client):
        r = await client.get("/api/v1/openapi.json")
        assert r.status_code != 401

    async def test_docs_no_auth_required(self, client):
        r = await client.get("/api/v1/docs")
        assert r.status_code != 401

    async def test_options_preflight_bypasses_auth(self, client):
        r = await client.options(
            "/api/v1/anything",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code != 401


class TestBearerAuthRejection:
    async def test_missing_authorization_header_returns_401(self, client):
        r = await client.get("/api/v1/anything")
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers
        assert "Bearer" in r.headers["WWW-Authenticate"]

    async def test_malformed_authorization_header_returns_401(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": "NotBearer xyz"},
        )
        assert r.status_code == 401

    async def test_empty_bearer_token_returns_401(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": "Bearer "},
        )
        assert r.status_code == 401

    async def test_wrong_token_returns_401(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": "Bearer wrong-token-xxxxxxxxxxxxxxxxx"},
        )
        assert r.status_code == 401

    async def test_correct_token_passes_auth(self, client):
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # Auth passed; route doesn't exist → 404.
        assert r.status_code == 404


class TestBearerAuthOQ2Loopback:
    async def test_loopback_client_can_post_to_platforms_auth(self, loopback_client):
        r = await loopback_client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code != 403

    async def test_external_client_blocked_from_platforms_auth(self, external_client):
        r = await external_client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 403


class TestBearerAuthLogging:
    async def test_no_raw_token_in_logs(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        secret = "VERY_SECRET_TOKEN_NEVER_LEAK_aa"  # noqa: S105  test sentinel, not a credential
        await client.get(
            "/api/v1/anything",
            headers={"Authorization": f"Bearer {secret}"},
        )
        out = capsys.readouterr().out
        assert secret not in out

    async def test_auth_rejected_event_emits_with_sha256_prefix(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        await client.get(
            "/api/v1/anything",
            headers={"Authorization": "Bearer wrong-token-xxxxxxxxxxxxxxxxx"},
        )
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        rejected = [e for e in events if e.get("event") == "api.auth.rejected"]
        assert len(rejected) >= 1
        e = rejected[0]
        if e.get("reason") == "bad_token":
            assert "token_sha256_prefix" in e
            assert len(e["token_sha256_prefix"]) == 8
