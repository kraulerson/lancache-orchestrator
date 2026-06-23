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


async def test_library_enumerate_forces_refresh_and_retries_on_401(monkeypatch, tmp_path):
    """A 401 from the downstream library call must force a token refresh and
    retry once — the documented 401-forces-refresh contract (audit 2026-06-09)."""
    s = _settings(tmp_path)
    ep_oauth.save_refresh_token(str(s.epic_session_path), "RT")

    refresh_calls: list[str] = []

    async def fake_refresh(rt, settings):
        refresh_calls.append(rt)
        # never-expiring access token, so only a 401 (not proactive expiry)
        # can trigger the second refresh.
        return AuthTokens(f"AT{len(refresh_calls)}", "RT", "acc", "Karl", "")

    enum_calls: list[str] = []

    async def fake_enum(at, settings):
        enum_calls.append(at)
        if len(enum_calls) == 1:
            raise ep_lib.EpicLibraryError("epic library fetch failed: HTTP 401", status_code=401)
        return [EpicLibraryItem("A", "ns", "c", "A")]

    monkeypatch.setattr(ep_oauth, "refresh", fake_refresh)
    monkeypatch.setattr(ep_lib, "enumerate_library", fake_enum)

    client = EpicClient(s)
    items = await client.library_enumerate()

    assert [i.app_name for i in items] == ["A"]
    assert len(enum_calls) == 2  # retried after the 401
    assert len(refresh_calls) == 2  # initial + forced-on-401


async def test_library_enumerate_does_not_retry_on_non_401(monkeypatch, tmp_path):
    """A non-401 downstream error must NOT trigger a refresh/retry loop."""
    s = _settings(tmp_path)
    ep_oauth.save_refresh_token(str(s.epic_session_path), "RT")

    refresh_calls: list[str] = []

    async def fake_refresh(rt, settings):
        refresh_calls.append(rt)
        return AuthTokens("AT", "RT", "acc", "Karl", "")

    async def fake_enum(at, settings):
        raise ep_lib.EpicLibraryError("epic library fetch failed: HTTP 503", status_code=503)

    monkeypatch.setattr(ep_oauth, "refresh", fake_refresh)
    monkeypatch.setattr(ep_lib, "enumerate_library", fake_enum)

    client = EpicClient(s)
    with pytest.raises(ep_lib.EpicLibraryError):
        await client.library_enumerate()
    assert len(refresh_calls) == 1  # no forced refresh on a non-401


async def test_concurrent_access_token_refreshes_once(monkeypatch, tmp_path):
    """COR-5 (review 2026-06-23): concurrent callers that both find the access
    token stale must serialize on a single refresh — Epic refresh tokens rotate
    (single-use), so a double refresh with the same token double-spends it and
    loses the session. The second caller must reuse the freshly-refreshed token."""
    import asyncio

    s = _settings(tmp_path)
    ep_oauth.save_refresh_token(str(s.epic_session_path), "RT")
    refresh_calls = {"n": 0}

    async def fake_refresh(rt, settings):
        refresh_calls["n"] += 1
        await asyncio.sleep(0.02)  # yield so the sibling coroutine interleaves
        return AuthTokens("AT", "RT2", "acc", "Karl", "")

    monkeypatch.setattr(ep_oauth, "refresh", fake_refresh)
    client = EpicClient(s)

    # Two concurrent token fetches on a cold client: the serialized impl refreshes
    # ONCE and hands the same token to both. The unlocked impl refreshes twice.
    results = await asyncio.gather(client._access_token(), client._access_token())
    assert refresh_calls["n"] == 1, f"refreshed {refresh_calls['n']} times (double-spend)"
    assert results == ["AT", "AT"]
