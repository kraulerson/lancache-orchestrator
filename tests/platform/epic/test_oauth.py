"""F6: Epic OAuth exchange/refresh + refresh-token persistence."""

from __future__ import annotations

import os

import httpx
import pytest

from orchestrator.core.settings import Settings
from orchestrator.platform.epic import oauth as ep_oauth
from orchestrator.platform.epic.models import AuthTokens

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


def _settings() -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN)


def _token_response(account="acc", display="Karl") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "ACCESS",
            "refresh_token": "REFRESH",
            "account_id": account,
            "displayName": display,
            "expires_at": "2026-06-03T01:00:00.000Z",
        },
    )


async def test_exchange_code_returns_tokens(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/oauth/token")
        body = req.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code=THECODE" in body
        return _token_response()

    monkeypatch.setattr(ep_oauth, "_build_transport", lambda: httpx.MockTransport(handler))
    tokens = await ep_oauth.exchange_code("THECODE", _settings())
    assert isinstance(tokens, AuthTokens)
    assert tokens.access_token == "ACCESS"
    assert tokens.refresh_token == "REFRESH"
    assert tokens.account_id == "acc"


async def test_refresh_uses_refresh_token(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "grant_type=refresh_token" in req.content.decode()
        return _token_response()

    monkeypatch.setattr(ep_oauth, "_build_transport", lambda: httpx.MockTransport(handler))
    tokens = await ep_oauth.refresh("OLDREFRESH", _settings())
    assert tokens.access_token == "ACCESS"


async def test_refresh_failure_raises_epicauth(monkeypatch):
    monkeypatch.setattr(
        ep_oauth,
        "_build_transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(400, json={"errorCode": "x"})),
    )
    with pytest.raises(ep_oauth.EpicAuthError):
        await ep_oauth.refresh("BAD", _settings())


def test_persist_and_load_refresh_token(tmp_path):
    path = str(tmp_path / "epic_session.json")
    ep_oauth.save_refresh_token(path, "RT-123")
    assert ep_oauth.load_refresh_token(path) == "RT-123"
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_load_missing_returns_none(tmp_path):
    assert ep_oauth.load_refresh_token(str(tmp_path / "nope.json")) is None
