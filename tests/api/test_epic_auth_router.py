"""F6: POST/GET /api/v1/platforms/epic/auth."""

from __future__ import annotations

import pytest

from orchestrator.api.routers import epic_auth as ea
from orchestrator.platform.epic.models import AuthTokens
from orchestrator.platform.epic.oauth import EpicAuthError

VALID_TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}

pytestmark = pytest.mark.asyncio


async def test_submit_auth_exchanges_persists_and_enqueues(client, populated_pool, monkeypatch):
    async def fake_exchange(code, settings):
        assert code == "THECODE"
        return AuthTokens("AT", "RT", "acc-123", "Karl", "2026-06-03T01:00:00Z")

    monkeypatch.setattr(ea, "exchange_code", fake_exchange)
    monkeypatch.setattr(ea, "save_refresh_token", lambda _p, _t: None)

    r = await client.post("/api/v1/platforms/epic/auth", headers=AUTH, json={"code": "THECODE"})
    assert r.status_code == 202
    body = r.json()
    assert body["account_id"] == "acc-123"
    assert body["display_name"] == "Karl"
    # No tokens echoed.
    assert "access_token" not in body and "refresh_token" not in body

    row = await populated_pool.read_one("SELECT auth_status FROM platforms WHERE name='epic'")
    assert row["auth_status"] == "ok"
    job = await populated_pool.read_one(
        "SELECT id FROM jobs WHERE kind='library_sync' AND platform='epic' AND state='queued'"
    )
    assert job is not None


async def test_submit_auth_save_token_oserror_returns_clean_error(
    client, populated_pool, monkeypatch
):
    """If persisting the refresh token fails (read-only/full FS, symlink at the
    path), the endpoint must return a clean error — not let the OSError escape as
    an unhandled 500 — and must never reflect the tokens (audit 2026-06-09)."""

    async def fake_exchange(code, settings):
        return AuthTokens("AT_SECRET", "RT_SECRET", "acc-123", "Karl", "")

    def boom(_p, _t):
        raise OSError("[Errno 30] Read-only file system")

    monkeypatch.setattr(ea, "exchange_code", fake_exchange)
    monkeypatch.setattr(ea, "save_refresh_token", boom)

    r = await client.post("/api/v1/platforms/epic/auth", headers=AUTH, json={"code": "THECODE"})
    assert r.status_code == 503
    assert "detail" in r.json()
    assert "AT_SECRET" not in r.text and "RT_SECRET" not in r.text


async def test_submit_auth_bad_code_returns_401(client, monkeypatch):
    async def fake_exchange(code, settings):
        raise EpicAuthError("rejected")

    monkeypatch.setattr(ea, "exchange_code", fake_exchange)
    r = await client.post("/api/v1/platforms/epic/auth", headers=AUTH, json={"code": "BAD"})
    assert r.status_code == 401


async def test_submit_auth_missing_bearer_returns_401(client):
    r = await client.post("/api/v1/platforms/epic/auth", json={"code": "X"})
    assert r.status_code == 401


async def test_get_auth_status(client, populated_pool):
    r = await client.get("/api/v1/platforms/epic/auth", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "auth_status" in body
    assert body["auth_status"] in ("ok", "expired", "error", "never")


async def test_submit_auth_rejects_extra_fields(client):
    """SEC-4 (review 2026-06-23): AuthCodeBody must reject unknown fields
    (extra='forbid'), matching the input-validation convention of the other
    request bodies. The app's RequestValidationError handler maps these to 400."""
    r = await client.post(
        "/api/v1/platforms/epic/auth",
        headers=AUTH,
        json={"code": "THECODE", "unexpected": "x"},
    )
    assert r.status_code == 400
