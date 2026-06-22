"""re-arch ③c: the legacy Steam worker routes are gone.

After deleting the ValvePython worker, the Steam auth endpoints and the
manifest-fetch trigger no longer exist. Authenticated requests to them must
return 404 (route absent), not 401/405/500 — a regression guard so the routes
can't quietly come back. Epic auth lives in a separate router and is untouched.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32
_AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


async def test_steam_auth_begin_removed(client):
    r = await client.post(
        "/api/v1/platforms/steam/auth", headers=_AUTH, json={"username": "u", "password": "p"}
    )
    assert r.status_code == 404


async def test_steam_auth_status_removed(client):
    r = await client.get("/api/v1/platforms/steam/auth/status", headers=_AUTH)
    assert r.status_code == 404


async def test_manifest_fetch_trigger_removed(client):
    r = await client.post("/api/v1/games/5/manifest/fetch", headers=_AUTH)
    assert r.status_code == 404


async def test_epic_auth_router_still_present(client):
    """Epic auth is a separate router and must survive the Steam worker deletion.
    GET returns the epic auth status, proving the route still exists."""
    r = await client.get("/api/v1/platforms/epic/auth", headers=_AUTH)
    assert r.status_code != 404
