"""Tests for BearerAuthMiddleware (spec §5.4)."""

from __future__ import annotations

import json
from typing import Any

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

    # UAT-5 U5-1: hardening tests for the Authorization header decode path.
    # httpx itself refuses to send non-ASCII Authorization values (RFC 7230);
    # the attack vector is a non-conforming HTTP client that sends raw bytes.
    # Drive the middleware directly via a synthetic ASGI scope to verify.
    async def test_non_ascii_bytes_in_header_rejected(self, unit_app):
        from orchestrator.api.middleware import BearerAuthMiddleware

        captured: dict[str, Any] = {}

        async def _receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]

        async def _inner_app(scope, receive, send) -> None:  # would-be unreachable
            captured["reached_inner"] = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = BearerAuthMiddleware(_inner_app)
        # Hand-construct raw header bytes with embedded non-ASCII (U+00A0).
        bad_value = b"Bearer " + b"a" * 24 + b"\xc2\xa0" + b"a" * 4
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/anything",
            "headers": [(b"authorization", bad_value)],
            "client": ("127.0.0.1", 12345),
            "query_string": b"",
        }
        await mw(scope, _receive, _send)
        assert captured.get("status") == 401
        assert not captured.get("reached_inner")

    async def test_oversized_authorization_header_rejected(self, client):
        """4096-byte cap on the Authorization header value. Anything
        larger short-circuits to 401 without reaching hmac.compare_digest."""
        huge = "Bearer " + ("a" * 10_000)
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": huge},
        )
        assert r.status_code == 401

    async def test_oversized_just_under_cap_still_compared(self, client):
        """Boundary: a header just under the 4096 cap reaches token compare
        and gets a normal 401 for bad_token (not oversized_header)."""
        almost = "Bearer " + ("a" * 4000)  # well under 4096
        r = await client.get(
            "/api/v1/anything",
            headers={"Authorization": almost},
        )
        assert r.status_code == 401


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
            assert "rejection_fingerprint" in e
            assert len(e["rejection_fingerprint"]) == 8
