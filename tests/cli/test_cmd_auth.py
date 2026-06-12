"""F11: auth subcommands. Creds/codes are prompted, never echoed/logged."""

from __future__ import annotations

import httpx


def test_auth_steam_no_2fa_success(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/platforms/steam/auth"
        return httpx.Response(200, json={"steam_id": 123})

    r = mock(["auth", "steam"], handler, input="user\nzQ9-secret-pw\n")
    assert r.exit_code == 0
    assert "SUCCESS" in r.output.upper()
    assert "zQ9-secret-pw" not in r.output  # hide_input: never echo the password


def test_auth_steam_2fa_challenge_then_complete(mock):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                202,
                json={
                    "challenge_id": "C1",
                    "challenge_type": "mobile_authenticator",
                    "expires_at": "2026-06-07T00:00:00Z",
                },
            )
        assert req.url.path == "/api/v1/platforms/steam/auth/C1"
        return httpx.Response(200, json={"steam_id": 9})

    r = mock(["auth", "steam"], handler, input="user\npass\nABCDE\n")
    assert r.exit_code == 0
    assert "SUCCESS" in r.output.upper()
    assert "ABCDE" not in r.output  # never echo the code


def test_auth_epic_success(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        import json

        assert json.loads(req.content) == {"code": "EPICCODE"}
        return httpx.Response(202, json={"account_id": "a", "display_name": "Karl"})

    r = mock(["auth", "epic"], handler, input="EPICCODE\n")
    assert r.exit_code == 0
    assert "Karl" in r.output
    assert "EPICCODE" not in r.output


def test_auth_status_table(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "platforms": [
                    {
                        "name": "steam",
                        "auth_status": "ok",
                        "auth_method": "steam_cm",
                        "auth_expires_at": None,
                        "last_sync_at": "2026-06-07",
                        "last_error": None,
                    },
                    {
                        "name": "epic",
                        "auth_status": "never",
                        "auth_method": "epic_oauth",
                        "auth_expires_at": None,
                        "last_sync_at": None,
                        "last_error": None,
                    },
                ],
                "meta": {"total": 2},
            },
        )

    r = mock(["auth", "status"], handler)
    assert r.exit_code == 0
    assert "STEAM" in r.output.upper() and "OK" in r.output.upper()
    assert "NEVER" in r.output.upper()
