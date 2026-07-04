"""Tests for the prefill-exclusions override API (#225)."""

from __future__ import annotations

VALID_TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


class TestPrefillExclusions:
    async def test_empty_list(self, client, populated_pool):
        r = await client.get("/api/v1/prefill-exclusions", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"exclusions": [], "total": 0}

    async def test_set_allow_and_list(self, client, populated_pool):
        r = await client.post(
            "/api/v1/prefill-exclusions/steam/440", headers=AUTH, json={"mode": "allow"}
        )
        assert r.status_code == 200
        body = (await client.get("/api/v1/prefill-exclusions", headers=AUTH)).json()
        assert body["total"] == 1
        row = body["exclusions"][0]
        assert row["platform"] == "steam"
        assert row["app_id"] == "440"
        assert row["mode"] == "allow"
        assert row["source"] == "operator"

    async def test_set_upserts_mode(self, client, populated_pool):
        await client.post(
            "/api/v1/prefill-exclusions/steam/1", headers=AUTH, json={"mode": "exclude"}
        )
        await client.post(
            "/api/v1/prefill-exclusions/steam/1", headers=AUTH, json={"mode": "allow"}
        )
        body = (await client.get("/api/v1/prefill-exclusions", headers=AUTH)).json()
        assert body["total"] == 1  # upsert, not duplicate
        assert body["exclusions"][0]["mode"] == "allow"

    async def test_unknown_platform_400(self, client, populated_pool):
        r = await client.post(
            "/api/v1/prefill-exclusions/gog/1", headers=AUTH, json={"mode": "allow"}
        )
        assert r.status_code == 400

    async def test_bad_mode_422(self, client, populated_pool):
        r = await client.post(
            "/api/v1/prefill-exclusions/steam/1", headers=AUTH, json={"mode": "nope"}
        )
        assert r.status_code == 400  # global RequestValidationError handler → 400

    async def test_delete_clears(self, client, populated_pool):
        await client.post(
            "/api/v1/prefill-exclusions/steam/1", headers=AUTH, json={"mode": "exclude"}
        )
        r = await client.delete("/api/v1/prefill-exclusions/steam/1", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["deleted"] == 1
        body = (await client.get("/api/v1/prefill-exclusions", headers=AUTH)).json()
        assert body["total"] == 0

    async def test_no_token_401(self, client, populated_pool):
        assert (await client.get("/api/v1/prefill-exclusions")).status_code == 401
