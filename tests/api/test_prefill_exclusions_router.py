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


class TestGameshelfReconcile:
    """PUT /prefill-exclusions/gameshelf/{platform} — self-healing reconcile of the
    source='gameshelf' exclude rows (Piece 3, #446). Game_shelf pushes the full set
    of app_ids covered on a higher-priority launcher; the endpoint makes the
    gameshelf-sourced rows exactly match, never touching operator/classifier rows."""

    async def _exclusions(self, client):
        body = (await client.get("/api/v1/prefill-exclusions", headers=AUTH)).json()
        return {row["app_id"]: row for row in body["exclusions"]}

    async def test_inserts_gameshelf_exclusions(self, client, populated_pool):
        r = await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic",
            headers=AUTH,
            json={"app_ids": ["ns1", "ns2"]},
        )
        assert r.status_code == 200
        rows = await self._exclusions(client)
        assert set(rows) == {"ns1", "ns2"}
        for row in rows.values():
            assert row["platform"] == "epic"
            assert row["mode"] == "exclude"
            assert row["source"] == "gameshelf"

    async def test_removes_stale_gameshelf_rows(self, client, populated_pool):
        await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic",
            headers=AUTH,
            json={"app_ids": ["a", "b", "c"]},
        )
        await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic",
            headers=AUTH,
            json={"app_ids": ["a", "b"]},
        )
        assert set(await self._exclusions(client)) == {"a", "b"}

    async def test_empty_list_clears_gameshelf(self, client, populated_pool):
        await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic",
            headers=AUTH,
            json={"app_ids": ["a", "b"]},
        )
        r = await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic", headers=AUTH, json={"app_ids": []}
        )
        assert r.status_code == 200
        assert await self._exclusions(client) == {}

    async def test_does_not_clobber_operator_allow(self, client, populated_pool):
        await client.post(
            "/api/v1/prefill-exclusions/epic/keep", headers=AUTH, json={"mode": "allow"}
        )
        r = await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic", headers=AUTH, json={"app_ids": ["keep"]}
        )
        assert r.status_code == 200
        rows = await self._exclusions(client)
        assert len(rows) == 1
        assert rows["keep"]["mode"] == "allow"
        assert rows["keep"]["source"] == "operator"

    async def test_does_not_delete_operator_or_classifier_rows(self, client, populated_pool):
        await client.post(
            "/api/v1/prefill-exclusions/epic/op", headers=AUTH, json={"mode": "exclude"}
        )
        await populated_pool.execute_write(
            "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
            "VALUES ('epic', 'cls', 'exclude', 'classifier')"
        )
        r = await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic", headers=AUTH, json={"app_ids": []}
        )
        assert r.status_code == 200
        rows = await self._exclusions(client)
        assert rows["op"]["source"] == "operator"
        assert rows["cls"]["source"] == "classifier"

    async def test_platform_scoped_delete(self, client, populated_pool):
        await client.put(
            "/api/v1/prefill-exclusions/gameshelf/steam", headers=AUTH, json={"app_ids": ["s1"]}
        )
        await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic", headers=AUTH, json={"app_ids": ["e1"]}
        )
        await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic", headers=AUTH, json={"app_ids": []}
        )
        assert set(await self._exclusions(client)) == {"s1"}

    async def test_idempotent_repeat(self, client, populated_pool):
        for _ in range(2):
            r = await client.put(
                "/api/v1/prefill-exclusions/gameshelf/epic",
                headers=AUTH,
                json={"app_ids": ["a", "b"]},
            )
            assert r.status_code == 200
        assert set(await self._exclusions(client)) == {"a", "b"}

    async def test_unknown_platform_400(self, client, populated_pool):
        r = await client.put(
            "/api/v1/prefill-exclusions/gameshelf/gog", headers=AUTH, json={"app_ids": []}
        )
        assert r.status_code == 400

    async def test_too_long_app_id_400(self, client, populated_pool):
        r = await client.put(
            "/api/v1/prefill-exclusions/gameshelf/epic",
            headers=AUTH,
            json={"app_ids": ["x" * 65]},
        )
        assert r.status_code == 400

    async def test_no_token_401(self, client, populated_pool):
        r = await client.put("/api/v1/prefill-exclusions/gameshelf/epic", json={"app_ids": []})
        assert r.status_code == 401
