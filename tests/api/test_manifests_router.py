"""Tests for GET /api/v1/manifests (BL9 / Feature 9 partial).

Covers spec - empty DB, happy path, pagination, filtering, sorting,
applied-echo, error paths, auth, pool-failure, and the NEW ?include=game
expansion convention.
"""

from __future__ import annotations

VALID_TOKEN = "a" * 32


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


class TestManifestsEmptyDb:
    async def test_empty_db_returns_empty_array(self, client, populated_pool):
        # populated_pool seeds 3 baseline manifests from db conftest; clear them
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM manifests")
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["manifests"] == []
        assert body["meta"]["total"] == 0
        assert body["meta"]["has_more"] is False
        assert body["meta"]["applied_includes"] == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestManifestsHappyPath:
    async def test_returns_manifests(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "manifests" in body
        assert "meta" in body
        assert len(body["manifests"]) > 0
        assert body["meta"]["total"] >= 20

    async def test_envelope_shape(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert set(body.keys()) == {"manifests", "meta"}
        assert set(body["meta"].keys()) == {
            "total",
            "limit",
            "offset",
            "has_more",
            "applied_filters",
            "applied_sort",
            "applied_includes",
        }

    async def test_per_manifest_field_set(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        expected_fields = {
            "id",
            "game_id",
            "version",
            "fetched_at",
            "chunk_count",
            "total_bytes",
            "depot_id",
            "game",
        }
        for manifest in body["manifests"]:
            assert set(manifest.keys()) == expected_fields
            # game must be null without ?include=game
            assert manifest["game"] is None


class TestManifestDepotIdExposure:
    """#127: migration 0003 added manifests.depot_id (populated by BL12), but the
    BL9 read endpoint's SELECT + response model predated the column, so every row
    came back depot_id=null. The endpoint must expose the stored value (and stay
    null for rows written before the column existed)."""

    async def test_depot_id_exposed_when_set(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM manifests")
            await tx.execute(
                "INSERT INTO manifests "
                "(game_id, version, fetched_at, chunk_count, total_bytes, depot_id, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "depot-set", "2026-06-01T00:00:00Z", 5, 100, 731, b"\x28\xb5\x2f\xfd"),
            )
            # A row predating the column (depot_id left NULL).
            await tx.execute(
                "INSERT INTO manifests "
                "(game_id, version, fetched_at, chunk_count, total_bytes, raw) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (1, "depot-null", "2026-06-02T00:00:00Z", 5, 100, b"\x28\xb5\x2f\xfd"),
            )

        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        by_version = {m["version"]: m for m in r.json()["manifests"]}
        assert by_version["depot-set"]["depot_id"] == 731
        assert by_version["depot-null"]["depot_id"] is None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestManifestsPagination:
    async def test_default_limit_50(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["meta"]["limit"] == 50
        assert body["meta"]["offset"] == 0

    async def test_explicit_limit(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=5",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["manifests"]) == 5
        assert body["meta"]["limit"] == 5

    async def test_offset_progression(self, client, manifests_pool_seeded):
        r1 = await client.get(
            "/api/v1/manifests?limit=5&offset=0",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.get(
            "/api/v1/manifests?limit=5&offset=5",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids1 = [m["id"] for m in r1.json()["manifests"]]
        ids2 = [m["id"] for m in r2.json()["manifests"]]
        assert set(ids1).isdisjoint(set(ids2))

    async def test_limit_above_max_returns_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=1000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestManifestsFilters:
    async def test_game_id_eq(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?game_id=1&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["game_id"] == 1

    async def test_game_id_in(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?game_id_in=1,2,3&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids = {m["game_id"] for m in r.json()["manifests"]}
        assert ids.issubset({1, 2, 3})

    async def test_version_eq(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?version=10001&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["version"] == "10001"

    async def test_version_in(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?version_in=10001,20001&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        versions = {m["version"] for m in r.json()["manifests"]}
        assert versions.issubset({"10001", "20001"})

    async def test_chunk_count_range(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?chunk_count_gte=1000&chunk_count_lte=5000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert 1000 <= m["chunk_count"] <= 5000

    async def test_total_bytes_gte(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?total_bytes_gte=50000000000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["total_bytes"] >= 50_000_000_000

    async def test_fetched_at_range(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?fetched_at_gte=2026-05-15T00:00:00Z&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["fetched_at"] >= "2026-05-15T00:00:00Z"


# ---------------------------------------------------------------------------
# Sort (default fetched_at:desc; tie-breaker id:asc)
# ---------------------------------------------------------------------------


class TestManifestsSort:
    async def test_default_fetched_at_desc(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        timestamps = [m["fetched_at"] for m in r.json()["manifests"]]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_default_applied_sort_has_tie_breaker(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied == [
            {"field": "fetched_at", "direction": "desc"},
            {"field": "id", "direction": "asc"},
        ]

    async def test_user_id_sort_dedupes_tie_breaker(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?sort=id:desc&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied == [{"field": "id", "direction": "desc"}]

    async def test_sort_by_total_bytes_desc(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?sort=total_bytes:desc&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        bytes_list = [m["total_bytes"] for m in r.json()["manifests"]]
        assert bytes_list == sorted(bytes_list, reverse=True)


# ---------------------------------------------------------------------------
# Applied echo (UAT-4 S2-A compact shape + new applied_includes)
# ---------------------------------------------------------------------------


class TestManifestsAppliedEcho:
    async def test_applied_filters_compact_dict_shape(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?game_id=1&chunk_count_gte=1000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert applied == {
            "game_id": {"eq": 1},
            "chunk_count": {"gte": 1000},
        }

    async def test_applied_includes_empty_by_default(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.json()["meta"]["applied_includes"] == []

    async def test_applied_includes_populated_when_requested(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=game&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.json()["meta"]["applied_includes"] == ["game"]


# ---------------------------------------------------------------------------
# ?include=game (NEW BL9 convention)
# ---------------------------------------------------------------------------


class TestManifestsIncludeGame:
    async def test_no_include_game_is_null(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=5",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["game"] is None

    async def test_include_game_populated(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=game&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["game"] is not None
            assert set(m["game"].keys()) == {"title", "platform", "app_id"}
            assert m["game"]["platform"] in ("steam", "epic")

    async def test_include_game_matches_seeded_game_row(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?game_id=1&include=game&limit=5",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # populated_pool seeds game id=1 as Counter-Strike (steam, app_id=10)
        for m in r.json()["manifests"]:
            assert m["game_id"] == 1
            assert m["game"]["title"] == "Counter-Strike"
            assert m["game"]["platform"] == "steam"
            assert m["game"]["app_id"] == "10"

    async def test_unknown_include_key_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "include keys not allowed" in r.json()["detail"]

    async def test_include_deduped(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=game,game,game&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        assert r.json()["meta"]["applied_includes"] == ["game"]

    async def test_empty_include_no_expansion(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.json()["meta"]["applied_includes"] == []
        for m in r.json()["manifests"]:
            assert m["game"] is None


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestManifestsErrorPaths:
    async def test_unknown_filter_field_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?password=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "unknown filter field" in r.json()["detail"]

    async def test_unknown_op_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?version_gte=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_unknown_sort_field_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?sort=password:desc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_invalid_timestamp_format_returns_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?fetched_at_gte=<script>alert(1)</script>",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auth + pool failure
# ---------------------------------------------------------------------------


class TestManifestsAuth:
    async def test_unauth_401(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests")
        assert r.status_code == 401


class TestManifestsPoolFailure:
    async def test_pool_error_returns_503(self, unit_app, client):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _FakeBrokenPool:
            async def read_one(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}
