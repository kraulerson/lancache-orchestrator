"""Tests for GET /api/v1/games (BL7 / Feature 9 partial).

Covers spec §5 — empty DB, happy path, pagination, filtering, sorting,
applied-echo, error paths, auth, pool-failure, metadata, last_error.
"""

from __future__ import annotations

import json

VALID_TOKEN = "a" * 32


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


class TestGamesEmptyDb:
    async def test_empty_db_returns_empty_array(self, client, populated_pool):
        # populated_pool has 5 games; clear them for this test
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM games")
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["games"] == []
        assert body["meta"]["total"] == 0
        assert body["meta"]["has_more"] is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestGamesHappyPath:
    async def test_returns_games(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "games" in body
        assert "meta" in body
        assert len(body["games"]) == 5  # populated_pool seeds 5
        assert body["meta"]["total"] == 5

    async def test_envelope_shape(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert set(body.keys()) == {"games", "meta"}
        assert set(body["meta"].keys()) == {
            "total",
            "limit",
            "offset",
            "has_more",
            "applied_filters",
            "applied_sort",
        }

    async def test_per_game_field_set(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert set(game.keys()) == {
                "id",
                "platform",
                "app_id",
                "title",
                "owned",
                "size_bytes",
                "current_version",
                "cached_version",
                "status",
                "last_validated_at",
                "last_prefilled_at",
                "last_error",
                "metadata",
            }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestGamesPagination:
    async def test_default_limit_50(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["games"]) == 50
        assert body["meta"]["limit"] == 50
        assert body["meta"]["offset"] == 0
        assert body["meta"]["total"] == 100
        assert body["meta"]["has_more"] is True

    async def test_explicit_limit(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["games"]) == 10
        assert body["meta"]["limit"] == 10
        assert body["meta"]["has_more"] is True

    async def test_offset_progression(self, client, games_pool_100):
        r1 = await client.get(
            "/api/v1/games?limit=10&offset=0",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.get(
            "/api/v1/games?limit=10&offset=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids1 = [g["id"] for g in r1.json()["games"]]
        ids2 = [g["id"] for g in r2.json()["games"]]
        assert set(ids1).isdisjoint(set(ids2))

    async def test_limit_above_max_returns_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?limit=1000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "limit" in r.json()["detail"]

    async def test_negative_offset_returns_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?offset=-1",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_has_more_false_on_last_page(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?limit=50&offset=50",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["games"]) == 50
        assert body["meta"]["has_more"] is False


# ---------------------------------------------------------------------------
# Filter: platform
# ---------------------------------------------------------------------------


class TestGamesFilterPlatform:
    async def test_eq_steam(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform=steam&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["platform"] == "steam"

    async def test_in_multi_value(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform_in=steam,epic&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        platforms = {g["platform"] for g in body["games"]}
        assert platforms == {"steam", "epic"}


# ---------------------------------------------------------------------------
# Filter: status
# ---------------------------------------------------------------------------


class TestGamesFilterStatus:
    async def test_eq_status(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?status=not_downloaded&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["status"] == "not_downloaded"


# ---------------------------------------------------------------------------
# Filter: owned
# ---------------------------------------------------------------------------


class TestGamesFilterOwned:
    async def test_owned_true(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?owned=1&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["owned"] == 1

    async def test_owned_false(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?owned=0&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["owned"] == 0


# ---------------------------------------------------------------------------
# Filter: size_bytes
# ---------------------------------------------------------------------------


class TestGamesFilterSizeBytes:
    async def test_gte(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?size_bytes_gte=50000000000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["size_bytes"] >= 50_000_000_000

    async def test_lte(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?size_bytes_lte=10000000000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["size_bytes"] is None or game["size_bytes"] <= 10_000_000_000

    async def test_gte_lte_combined(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?size_bytes_gte=10000000000&size_bytes_lte=50000000000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert 10_000_000_000 <= game["size_bytes"] <= 50_000_000_000


# ---------------------------------------------------------------------------
# Filter: time ranges
# ---------------------------------------------------------------------------


class TestGamesFilterTimeRange:
    async def test_last_prefilled_at_gte(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?last_prefilled_at_gte=2026-05-15&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["last_prefilled_at"] is not None
            assert game["last_prefilled_at"] >= "2026-05-15"


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------


class TestGamesSort:
    async def test_default_title_asc(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        titles = [g["title"] for g in r.json()["games"]]
        assert titles == sorted(titles)

    async def test_title_desc(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=title:desc&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        titles = [g["title"] for g in r.json()["games"]]
        assert titles == sorted(titles, reverse=True)

    async def test_size_bytes_desc(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=size_bytes:desc&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        sizes = [g["size_bytes"] for g in r.json()["games"] if g["size_bytes"] is not None]
        assert sizes == sorted(sizes, reverse=True)

    async def test_tie_breaker_in_applied_sort(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=title:asc&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied[-1] == {"field": "id", "direction": "asc"}

    async def test_user_id_sort_dedupes_tie_breaker(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=id:desc&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert len(applied) == 1
        assert applied[0] == {"field": "id", "direction": "desc"}


# ---------------------------------------------------------------------------
# Applied echo (meta.applied_filters / applied_sort)
# ---------------------------------------------------------------------------


class TestGamesAppliedEcho:
    async def test_applied_filters_reflects_parse(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform=steam&size_bytes_gte=1000&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert applied["platform"]["eq"] == "steam"
        assert applied["size_bytes"]["gte"] == 1000

    async def test_applied_filters_absent_for_unfiltered_fields(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform=steam&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert "status" not in applied
        assert "size_bytes" not in applied


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestGamesErrors:
    async def test_unknown_filter_field_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?password=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "unknown filter field" in r.json()["detail"]

    async def test_unknown_operator_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform_gte=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_invalid_value_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?size_bytes_gte=abc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_unknown_sort_field_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=password:desc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    # UAT-5 U5-8: enforce ?include= convention. games has no includable keys
    # so any ?include= value should 400.
    async def test_unknown_include_key_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?include=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "include" in r.json()["detail"].lower()

    async def test_empty_include_accepted(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?include=&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200  # empty include is a no-op


# ---------------------------------------------------------------------------
# Auth (smoke)
# ---------------------------------------------------------------------------


class TestGamesAuth:
    async def test_no_token_returns_401(self, client, games_pool_100):
        r = await client.get("/api/v1/games")
        assert r.status_code == 401

    async def test_valid_token_returns_200(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Pool failure
# ---------------------------------------------------------------------------


class TestGamesPoolFailure:
    async def test_pool_error_returns_503(self, unit_app, client):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _FakeBrokenPool:
            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

            async def read_one(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()

        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}


# ---------------------------------------------------------------------------
# metadata column
# ---------------------------------------------------------------------------


class TestGamesMetadata:
    async def test_well_formed_metadata_parsed(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE games SET metadata = ? WHERE id = 1",
                (json.dumps({"depots": [10, 11]}),),
            )
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["metadata"] == {"depots": [10, 11]}

    async def test_null_metadata_returns_null(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute("UPDATE games SET metadata = NULL WHERE id = 1")
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["metadata"] is None

    async def test_malformed_metadata_returns_null(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE games SET metadata = ? WHERE id = 1",
                ("not-a-valid-{json",),
            )
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["metadata"] is None

    # UAT-5 U5-3: defend against non-string raw_meta pool returns
    async def test_non_buffer_metadata_type_returns_null(self, client, populated_pool):
        from unittest.mock import patch

        async def _read_all_with_dict_meta(*_a, **_kw):
            return [
                {
                    "id": 1,
                    "platform": "steam",
                    "app_id": "10",
                    "title": "Counter-Strike",
                    "owned": 1,
                    "size_bytes": 1000,
                    "current_version": None,
                    "cached_version": None,
                    "status": "up_to_date",
                    "last_validated_at": None,
                    "last_prefilled_at": None,
                    "last_error": None,
                    "metadata": {"already-decoded": True},  # non-buffer type
                }
            ]

        async def _read_one(*_a, **_kw):
            return {"total": 1}

        with (
            patch("orchestrator.db.pool.Pool.read_all", new=_read_all_with_dict_meta),
            patch("orchestrator.db.pool.Pool.read_one", new=_read_one),
        ):
            r = await client.get(
                "/api/v1/games?limit=500",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 200  # NOT 500
        game = r.json()["games"][0]
        assert game["metadata"] is None

    # UAT-5 U5-2: Pydantic Literal[] crash on out-of-allow-list DB value
    async def test_out_of_literal_status_skips_row(self, client, populated_pool):
        from unittest.mock import patch

        async def _read_all_garbage_status(*_a, **_kw):
            return [
                {
                    "id": 1,
                    "platform": "steam",
                    "app_id": "10",
                    "title": "BAD ROW",
                    "owned": 1,
                    "size_bytes": 1000,
                    "current_version": None,
                    "cached_version": None,
                    "status": "garbage_value",  # not in Literal
                    "last_validated_at": None,
                    "last_prefilled_at": None,
                    "last_error": None,
                    "metadata": None,
                },
                {
                    "id": 2,
                    "platform": "steam",
                    "app_id": "20",
                    "title": "GOOD ROW",
                    "owned": 1,
                    "size_bytes": 2000,
                    "current_version": None,
                    "cached_version": None,
                    "status": "up_to_date",
                    "last_validated_at": None,
                    "last_prefilled_at": None,
                    "last_error": None,
                    "metadata": None,
                },
            ]

        async def _read_one(*_a, **_kw):
            return {"total": 2}

        with (
            patch("orchestrator.db.pool.Pool.read_all", new=_read_all_garbage_status),
            patch("orchestrator.db.pool.Pool.read_one", new=_read_one),
        ):
            r = await client.get(
                "/api/v1/games?limit=500",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 200  # NOT 500
        body = r.json()
        # bad row skipped; good row remains
        assert [g["id"] for g in body["games"]] == [2]
        # total still reflects DB count (the bad row exists, we just skipped it)
        assert body["meta"]["total"] == 2


# ---------------------------------------------------------------------------
# last_error truncation
# ---------------------------------------------------------------------------


class TestGamesLastErrorTruncation:
    async def test_null_passes_through(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute("UPDATE games SET last_error = NULL WHERE id = 1")
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["last_error"] is None

    async def test_truncated_at_200(self, client, populated_pool):
        long_err = "x" * 5000
        async with populated_pool.write_transaction() as tx:
            await tx.execute("UPDATE games SET last_error = ? WHERE id = 1", (long_err,))
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert len(game["last_error"]) == 200
