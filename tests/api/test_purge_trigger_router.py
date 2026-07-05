"""Tests for POST /api/v1/games/{game_id}/purge (F18)."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32

pytestmark = pytest.mark.asyncio


async def _ensure_steam_game(pool, *, app_id="730", title="CS2") -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) "
        "VALUES ('steam', ?, ?, 1) "
        "ON CONFLICT(platform, app_id) DO UPDATE SET title=excluded.title",
        (app_id, title),
    )
    row = await pool.read_one("SELECT id FROM games WHERE platform='steam' AND app_id=?", (app_id,))
    return row["id"]


class TestQueueJob:
    async def test_first_call_queues_purge_job_and_returns_202(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="9999", title="t")
        r = await client.post(
            f"/api/v1/games/{game_id}/purge",
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
            "kind": "purge",
            "game_id": game_id,
            "platform": "steam",
            "state": "queued",
            "source": "api",
        }

    async def test_epic_game_queues_with_epic_platform(self, client, populated_pool):
        row = await populated_pool.read_one("SELECT id FROM games WHERE platform='epic' LIMIT 1")
        assert row is not None
        r = await client.post(
            f"/api/v1/games/{row['id']}/purge",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        jrow = await populated_pool.read_one(
            "SELECT kind, game_id, platform, state, source FROM jobs WHERE id=?",
            (r.json()["job_id"],),
        )
        assert jrow == {
            "kind": "purge",
            "game_id": row["id"],
            "platform": "epic",
            "state": "queued",
            "source": "api",
        }


class TestDedup:
    async def test_concurrent_calls_return_same_job_id(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="dedup-p", title="t")
        r1 = await client.post(
            f"/api/v1/games/{game_id}/purge",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.post(
            f"/api/v1/games/{game_id}/purge",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["job_id"] == r2.json()["job_id"]

    async def test_new_job_after_existing_finished(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="fin-p", title="t")
        await populated_pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "VALUES ('purge', ?, 'steam', 'succeeded', 'api')",
            (game_id,),
        )
        r = await client.post(
            f"/api/v1/games/{game_id}/purge",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        row = await populated_pool.read_one(
            "SELECT state FROM jobs WHERE id=?", (r.json()["job_id"],)
        )
        assert row["state"] == "queued"


class TestErrors:
    async def test_unknown_game_returns_404(self, client):
        r = await client.post(
            "/api/v1/games/99999/purge",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"]

    async def test_unsupported_platform_returns_400(self, unit_app):
        """A platform outside steam/epic is rejected (defensive; a mock pool reaches
        the branch since the games CHECK constrains real rows to steam/epic)."""
        import httpx

        from orchestrator.api.dependencies import get_pool_dep

        class _GogPool:
            async def read_one(self, query, *_a, **_kw):
                if "FROM games" in query:
                    return {"id": 7, "platform": "gog"}
                return None

            async def execute_write(self, *_a, **_kw):
                return 1

        unit_app.dependency_overrides[get_pool_dep] = lambda: _GogPool()
        transport = httpx.ASGITransport(app=unit_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.post(
                "/api/v1/games/7/purge",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 400
        assert "gog" in r.json()["detail"]


class TestAuthBoundary:
    async def test_missing_bearer_returns_401(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="xp", title="x")
        r = await client.post(f"/api/v1/games/{game_id}/purge")
        assert r.status_code == 401

    async def test_wrong_bearer_returns_401(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="yp", title="y")
        r = await client.post(
            f"/api/v1/games/{game_id}/purge",
            headers={"Authorization": "Bearer wrong-token-of-32-chars-abcdefgh"},
        )
        assert r.status_code == 401
