"""Tests for POST /api/v1/platforms/steam/auth* (BL10 / F1)."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32


@pytest.fixture(autouse=True)
def _clear_challenge_state():
    """Clear module-level _challenge_expiries before and after each test.

    Prevents state leakage between tests that store challenge IDs in the
    router's module-level dict.
    """
    from orchestrator.api.routers.auth import _challenge_expiries

    _challenge_expiries.clear()
    yield
    _challenge_expiries.clear()


class TestAuthBegin:
    async def test_happy_path_no_2fa_returns_200(self, client, stub_steam_client):
        # Wire the stub into the auth router's DI
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "no_2fa"
        # Override the dep on whatever app the client was built against:
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "authenticated"
        assert body["steam_id"] == 76561198000000000

    async def test_needs_2fa_returns_202_with_challenge(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        assert r.status_code == 202
        body = r.json()
        assert "challenge_id" in body
        assert body["challenge_type"] == "mobile_authenticator"
        assert "expires_at" in body

    async def test_bad_credentials_returns_401(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "bad_credentials"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "wrong"},
        )
        assert r.status_code == 401

    async def test_missing_username_returns_400(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"password": "secret"},
        )
        assert r.status_code == 400

    async def test_unauth_returns_401(self, client):
        r = await client.post(
            "/api/v1/platforms/steam/auth",
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 401

    async def test_non_loopback_returns_403(self, external_client):
        r = await external_client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "u", "password": "p"},
        )
        assert r.status_code == 403

    async def test_no_password_in_logs(self, client, stub_steam_client, capsys):
        from orchestrator.api.routers.auth import get_steam_client_dep
        from orchestrator.core.logging import configure_logging

        configure_logging()
        stub_steam_client.scenario = "no_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        secret = "PASSWORD_DO_NOT_LEAK_aa"  # noqa: S105 test sentinel
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": secret},
        )
        out = capsys.readouterr().out
        assert secret not in out


class TestAuthComplete:
    async def test_good_code_returns_200(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        # First, begin auth so the server stores the challenge
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        # Now submit the code
        r = await client.post(
            "/api/v1/platforms/steam/auth/stub-challenge-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "12345"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "authenticated"

    async def test_bad_code_returns_401(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "needs_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        # Flip scenario for the complete call
        stub_steam_client.scenario = "bad_code"
        r = await client.post(
            "/api/v1/platforms/steam/auth/stub-challenge-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "wrong"},
        )
        assert r.status_code == 401

    async def test_unknown_challenge_returns_404(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.post(
            "/api/v1/platforms/steam/auth/no-such-id",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"code": "anything"},
        )
        assert r.status_code == 404


class TestAuthStatus:
    async def test_status_returns_authenticated_state(self, client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        r = await client.get(
            "/api/v1/platforms/steam/auth/status",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["authenticated"] is True
        assert body["steam_id"] == 76561198000000000

    async def test_status_not_loopback_only(self, external_client, stub_steam_client):
        from orchestrator.api.routers.auth import get_steam_client_dep

        external_client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: (
            stub_steam_client
        )
        # external_client is not loopback; status should still 200 (not 403)
        r = await external_client.get(
            "/api/v1/platforms/steam/auth/status",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code != 403


class TestPlatformsRowUpdates:
    async def test_successful_auth_updates_platforms_row(
        self, client, stub_steam_client, populated_pool
    ):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "no_2fa"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "secret"},
        )
        row = await populated_pool.read_one(
            "SELECT auth_status, last_sync_at, last_error, config FROM platforms WHERE name='steam'"
        )
        assert row["auth_status"] == "ok"
        assert row["last_sync_at"] is not None
        assert row["last_error"] is None
        import json as _json

        config = _json.loads(row["config"])
        assert config["steam_id"] == 76561198000000000
        assert config["username"] == "alice"
        # NEVER persist a token
        assert "password" not in row["config"]
        assert "token" not in row["config"]

    async def test_failed_auth_writes_last_error(self, client, stub_steam_client, populated_pool):
        from orchestrator.api.routers.auth import get_steam_client_dep

        stub_steam_client.scenario = "bad_credentials"
        client._transport.app.dependency_overrides[get_steam_client_dep] = lambda: stub_steam_client
        await client.post(
            "/api/v1/platforms/steam/auth",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            json={"username": "alice", "password": "wrong"},
        )
        row = await populated_pool.read_one(
            "SELECT auth_status, last_error FROM platforms WHERE name='steam'"
        )
        assert row["auth_status"] == "error"
        assert row["last_error"] is not None
        assert "InvalidCredentials" in row["last_error"]
