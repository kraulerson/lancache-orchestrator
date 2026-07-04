"""Tests for GET /api/v1/selection/candidates (#229 prefill-selection review)."""

from __future__ import annotations

VALID_TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


async def _seed_app_info(pool, rows):
    async with pool.write_transaction() as tx:
        for app_id, app_type, name in rows:
            await tx.execute(
                "INSERT INTO steam_app_info (app_id, app_type, name) VALUES (?, ?, ?)",
                (app_id, app_type, name),
            )


class TestSelectionCandidates:
    async def test_flags_non_games_and_named_tools(self, client, populated_pool):
        await _seed_app_info(
            populated_pool,
            [
                ("10", "game", "Counter-Strike"),
                ("220700", "application", "RPG Maker VX Ace"),
                ("323", "music", "Celeste Soundtrack"),
                ("90", "game", "Half-Life Dedicated Server"),
            ],
        )
        r = await client.get("/api/v1/selection/candidates", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["total_scanned"] == 4
        by_id = {c["app_id"]: c for c in body["candidates"]}
        assert "10" not in by_id  # real game is kept
        assert by_id["220700"]["reason"].startswith("type=application")
        assert by_id["323"]["reason"].startswith("type=music")
        assert "dedicated server" in by_id["90"]["reason"].lower()
        assert body["total_candidates"] == 3

    async def test_envelope_shape(self, client, populated_pool):
        await _seed_app_info(populated_pool, [("1", "music", "OST")])
        body = (await client.get("/api/v1/selection/candidates", headers=AUTH)).json()
        assert set(body.keys()) == {"candidates", "total_candidates", "total_scanned"}
        assert set(body["candidates"][0].keys()) == {"app_id", "name", "app_type", "reason"}

    async def test_empty_when_no_app_info(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM steam_app_info")
        r = await client.get("/api/v1/selection/candidates", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"candidates": [], "total_candidates": 0, "total_scanned": 0}

    async def test_no_token_returns_401(self, client, populated_pool):
        r = await client.get("/api/v1/selection/candidates")
        assert r.status_code == 401

    async def test_pool_error_returns_503(self, unit_app, client):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _FakeBrokenPool:
            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()
        r = await client.get("/api/v1/selection/candidates", headers=AUTH)
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}
