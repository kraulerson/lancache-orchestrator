"""F6: EpicClient token lifecycle + library/manifest facade."""

from __future__ import annotations

import pytest

from orchestrator.core.settings import Settings
from orchestrator.platform.epic import library as ep_lib
from orchestrator.platform.epic import oauth as ep_oauth
from orchestrator.platform.epic.client import EpicClient, EpicNotAuthenticatedError
from orchestrator.platform.epic.models import AuthTokens, EpicLibraryItem

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


def _settings(tmp_path) -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN, epic_session_path=tmp_path / "epic.json")


async def test_library_enumerate_refreshes_and_persists_rotated_token(monkeypatch, tmp_path):
    s = _settings(tmp_path)
    ep_oauth.save_refresh_token(str(s.epic_session_path), "RT")
    tokens = AuthTokens("AT", "RT2", "acc", "Karl", "2026-06-03T01:00:00Z")

    async def fake_refresh(rt, settings):
        assert rt == "RT"
        return tokens

    async def fake_enum(at, settings):
        assert at == "AT"
        return [EpicLibraryItem("A", "ns", "c", "A")]

    monkeypatch.setattr(ep_oauth, "refresh", fake_refresh)
    monkeypatch.setattr(ep_lib, "enumerate_library", fake_enum)

    client = EpicClient(s)
    items = await client.library_enumerate()
    assert [i.app_name for i in items] == ["A"]
    # rotated refresh token persisted for next boot
    assert ep_oauth.load_refresh_token(str(s.epic_session_path)) == "RT2"


async def test_no_stored_refresh_token_raises(tmp_path):
    client = EpicClient(_settings(tmp_path))
    with pytest.raises(EpicNotAuthenticatedError):
        await client.library_enumerate()


async def test_refresh_rejected_raises_not_authenticated(monkeypatch, tmp_path):
    s = _settings(tmp_path)
    ep_oauth.save_refresh_token(str(s.epic_session_path), "RT")

    async def fake_refresh(rt, settings):
        raise ep_oauth.EpicAuthError("rejected")

    monkeypatch.setattr(ep_oauth, "refresh", fake_refresh)
    with pytest.raises(EpicNotAuthenticatedError):
        await EpicClient(s).library_enumerate()


async def test_expired_cached_token_triggers_refresh(monkeypatch, tmp_path):
    s = _settings(tmp_path)
    ep_oauth.save_refresh_token(str(s.epic_session_path), "RT")
    calls = {"n": 0}

    async def fake_refresh(rt, settings):
        calls["n"] += 1
        # already-expired access token (past) so the next call refreshes again
        return AuthTokens(f"AT{calls['n']}", "RT", "acc", "Karl", "2000-01-01T00:00:00Z")

    async def fake_enum(at, settings):
        return []

    monkeypatch.setattr(ep_oauth, "refresh", fake_refresh)
    monkeypatch.setattr(ep_lib, "enumerate_library", fake_enum)

    client = EpicClient(s)
    await client.library_enumerate()  # refresh #1
    await client.library_enumerate()  # cached token expired -> refresh #2
    assert calls["n"] == 2
