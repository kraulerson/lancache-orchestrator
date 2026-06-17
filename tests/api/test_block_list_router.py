"""Tests for the block-list REST resource (F8): GET / POST / DELETE."""

from __future__ import annotations

VALID_TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


class TestBlockListPost:
    async def test_post_creates_returns_201(self, client, populated_pool):
        r = await client.post(
            "/api/v1/block-list",
            json={"platform": "steam", "app_id": "730", "reason": "no"},
            headers=AUTH,
        )
        assert r.status_code == 201
        body = r.json()
        assert body["platform"] == "steam" and body["app_id"] == "730"
        assert body["reason"] == "no" and body["source"] == "api"
        assert set(body) == {"id", "platform", "app_id", "reason", "source", "blocked_at"}

    async def test_post_idempotent_returns_200(self, client, populated_pool):
        await client.post(
            "/api/v1/block-list", json={"platform": "steam", "app_id": "730"}, headers=AUTH
        )
        r = await client.post(
            "/api/v1/block-list", json={"platform": "steam", "app_id": "730"}, headers=AUTH
        )
        assert r.status_code == 200

    async def test_post_accepts_unknown_app_id_preblock(self, client, populated_pool):
        r = await client.post(
            "/api/v1/block-list", json={"platform": "epic", "app_id": "never-seen"}, headers=AUTH
        )
        assert r.status_code == 201

    async def test_post_rejects_extra_field_400(self, client, populated_pool):
        # app remaps FastAPI's default 422 -> 400 (F9 convention)
        r = await client.post(
            "/api/v1/block-list",
            json={"platform": "steam", "app_id": "1", "nope": 1},
            headers=AUTH,
        )
        assert r.status_code == 400

    async def test_post_rejects_bad_platform_400(self, client, populated_pool):
        r = await client.post(
            "/api/v1/block-list", json={"platform": "gog", "app_id": "1"}, headers=AUTH
        )
        assert r.status_code == 400

    async def test_post_requires_auth_401(self, client, populated_pool):
        r = await client.post("/api/v1/block-list", json={"platform": "steam", "app_id": "1"})
        assert r.status_code == 401


class TestBlockListGet:
    async def test_get_empty_envelope(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM block_list")
        r = await client.get("/api/v1/block-list", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["block_list"] == [] and body["meta"]["total"] == 0

    async def test_get_filter_by_platform(self, client, populated_pool):
        for p, a in [("steam", "1"), ("epic", "2")]:
            await client.post("/api/v1/block-list", json={"platform": p, "app_id": a}, headers=AUTH)
        r = await client.get("/api/v1/block-list?platform=steam", headers=AUTH)
        rows = r.json()["block_list"]
        assert [x["platform"] for x in rows] == ["steam"]

    async def test_get_rejects_unknown_filter_400(self, client, populated_pool):
        r = await client.get("/api/v1/block-list?bogus=1", headers=AUTH)
        assert r.status_code == 400


class TestBlockListDelete:
    async def test_delete_present_removes_1(self, client, populated_pool):
        await client.post(
            "/api/v1/block-list", json={"platform": "steam", "app_id": "9"}, headers=AUTH
        )
        r = await client.delete("/api/v1/block-list/steam/9", headers=AUTH)
        assert r.status_code == 200 and r.json() == {"removed": 1}

    async def test_delete_absent_idempotent_removes_0(self, client, populated_pool):
        r = await client.delete("/api/v1/block-list/steam/absent", headers=AUTH)
        assert r.status_code == 200 and r.json() == {"removed": 0}
