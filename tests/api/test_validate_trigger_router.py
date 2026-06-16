"""Tests for POST /api/v1/games/{game_id}/validate (F7)."""

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
    async def test_first_call_queues_job_and_returns_202(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="9999", title="t")
        r = await client.post(
            f"/api/v1/games/{game_id}/validate",
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
            "kind": "validate",
            "game_id": game_id,
            "platform": "steam",
            "state": "queued",
            "source": "api",
        }


class TestDedup:
    async def test_concurrent_calls_return_same_job_id(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="dedup-v", title="t")
        r1 = await client.post(
            f"/api/v1/games/{game_id}/validate",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.post(
            f"/api/v1/games/{game_id}/validate",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["job_id"] == r2.json()["job_id"]

    async def test_concurrent_triggers_across_games_return_correct_per_game_id(
        self, client, populated_pool
    ):
        """#123.3 regression: concurrent validate POSTs for DIFFERENT games must
        each return a job_id belonging to THAT game — never another game's id from
        a racy global re-select. The original issue feared a global
        `ORDER BY id DESC LIMIT 1` re-read could return the wrong id under
        concurrency; the current code instead re-selects filtered by
        (kind, game_id, in-flight state), backed by the migration-0006 partial
        UNIQUE index + `INSERT ... ON CONFLICT DO NOTHING`, so the returned id is
        deterministic per game. (`lastrowid` — the issue's proposed fix — would be
        unreliable here, since a no-op ON CONFLICT INSERT leaves it stale.) This
        locks that invariant in."""
        import asyncio

        game_ids = [
            await _ensure_steam_game(populated_pool, app_id=f"conc-{i}", title=f"g{i}")
            for i in range(6)
        ]

        async def post(gid: int):
            r = await client.post(
                f"/api/v1/games/{gid}/validate",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
            return gid, r

        results = await asyncio.gather(*(post(gid) for gid in game_ids))

        for gid, r in results:
            assert r.status_code == 202
            job_id = r.json()["job_id"]
            row = await populated_pool.read_one("SELECT game_id FROM jobs WHERE id=?", (job_id,))
            assert row is not None and row["game_id"] == gid, (
                f"trigger for game {gid} returned job {job_id} "
                f"belonging to game {row and row['game_id']}"
            )

    async def test_new_job_after_existing_finished(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="fin-v", title="t")
        await populated_pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "VALUES ('validate', ?, 'steam', 'succeeded', 'api')",
            (game_id,),
        )
        r = await client.post(
            f"/api/v1/games/{game_id}/validate",
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
            "/api/v1/games/99999/validate",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"]

    async def test_non_steam_game_returns_400(self, client, populated_pool):
        row = await populated_pool.read_one("SELECT id FROM games WHERE platform='epic' LIMIT 1")
        assert row is not None
        r = await client.post(
            f"/api/v1/games/{row['id']}/validate",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "steam" in r.json()["detail"]


class TestAuthBoundary:
    async def test_missing_bearer_returns_401(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="xv", title="x")
        r = await client.post(f"/api/v1/games/{game_id}/validate")
        assert r.status_code == 401

    async def test_wrong_bearer_returns_401(self, client, populated_pool):
        game_id = await _ensure_steam_game(populated_pool, app_id="yv", title="y")
        r = await client.post(
            f"/api/v1/games/{game_id}/validate",
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
                "/api/v1/games/1/validate",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 503
