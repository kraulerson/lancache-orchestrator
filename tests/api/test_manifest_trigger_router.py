"""Tests for POST /api/v1/games/{game_id}/manifest/fetch (BL12)."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32


pytestmark = pytest.mark.asyncio


async def _ensure_steam_game(pool, *, app_id="730", title="CS2") -> int:
    """Upsert a steam game. Returns its id."""
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) "
        "VALUES ('steam', ?, ?, 1) "
        "ON CONFLICT(platform, app_id) DO UPDATE SET title=excluded.title",
        (app_id, title),
    )
    row = await pool.read_one("SELECT id FROM games WHERE platform='steam' AND app_id=?", (app_id,))
    return row["id"]


class TestQueueJob:
    async def test_first_call_queues_job_and_returns_202(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="9999", title="t")
        r = await client.post(
            f"/api/v1/games/{game_id}/manifest/fetch",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        body = r.json()
        assert "job_id" in body
        row = await populated_pool.read_one(
            "SELECT kind, game_id, platform, state, source FROM jobs WHERE id=?",
            (body["job_id"],),
        )
        assert row == {
            "kind": "manifest_fetch",
            "game_id": game_id,
            "platform": "steam",
            "state": "queued",
            "source": "api",
        }


class TestDedup:
    async def test_concurrent_calls_return_same_job_id(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="dedup-test", title="t")
        r1 = await client.post(
            f"/api/v1/games/{game_id}/manifest/fetch",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.post(
            f"/api/v1/games/{game_id}/manifest/fetch",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["job_id"] == r2.json()["job_id"]

    async def test_different_games_get_distinct_job_ids(self, client, populated_pool):
        a = await _ensure_steam_game(populated_pool, app_id="alpha", title="A")
        b = await _ensure_steam_game(populated_pool, app_id="beta", title="B")
        r1 = await client.post(
            f"/api/v1/games/{a}/manifest/fetch",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.post(
            f"/api/v1/games/{b}/manifest/fetch",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r1.json()["job_id"] != r2.json()["job_id"]

    async def test_new_job_after_existing_finished(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="finished-test", title="t")
        # Pre-seed a succeeded manifest_fetch for this game.
        await populated_pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "VALUES ('manifest_fetch', ?, 'steam', 'succeeded', 'api')",
            (game_id,),
        )
        r = await client.post(
            f"/api/v1/games/{game_id}/manifest/fetch",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        new_id = r.json()["job_id"]
        row = await populated_pool.read_one("SELECT state FROM jobs WHERE id=?", (new_id,))
        assert row["state"] == "queued"


class TestErrors:
    async def test_unknown_game_returns_404(self, client):
        r = await client.post(
            "/api/v1/games/99999/manifest/fetch",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"]

    async def test_non_steam_game_returns_400(self, client, populated_pool):
        # populated_pool seeds an epic game (fortnite).
        row = await populated_pool.read_one("SELECT id FROM games WHERE platform='epic' LIMIT 1")
        assert row is not None
        r = await client.post(
            f"/api/v1/games/{row['id']}/manifest/fetch",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "steam" in r.json()["detail"]


class TestAuthBoundary:
    async def test_missing_bearer_returns_401(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="x", title="x")
        r = await client.post(f"/api/v1/games/{game_id}/manifest/fetch")
        assert r.status_code == 401

    async def test_wrong_bearer_returns_401(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="y", title="y")
        r = await client.post(
            f"/api/v1/games/{game_id}/manifest/fetch",
            headers={"Authorization": "Bearer wrong-token-of-32-chars-abcdefgh"},
        )
        assert r.status_code == 401


class TestPoolFailure:
    async def test_db_failure_returns_503(self, unit_app):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _BrokenPool:
            async def read_one(self, *_a, **_kw):
                raise PoolError("simulated outage")

            async def execute_write(self, *_a, **_kw):
                raise PoolError("simulated outage")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _BrokenPool()
        import httpx

        transport = httpx.ASGITransport(app=unit_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.post(
                "/api/v1/games/1/manifest/fetch",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 503
