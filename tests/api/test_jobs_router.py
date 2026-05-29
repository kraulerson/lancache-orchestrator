"""Tests for GET /api/v1/jobs (BL8 / Feature 9 partial).

Covers spec §5 — empty DB, happy path, pagination, filtering, sorting,
applied-echo, error paths, auth, pool-failure, payload, error truncation.
"""

from __future__ import annotations

import json

VALID_TOKEN = "a" * 32


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


class TestJobsEmptyDb:
    async def test_empty_db_returns_empty_array(self, client, populated_pool):
        # populated_pool seeds a few baseline jobs; clear them for this test
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM jobs")
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["jobs"] == []
        assert body["meta"]["total"] == 0
        assert body["meta"]["has_more"] is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestJobsHappyPath:
    async def test_returns_jobs(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "jobs" in body
        assert "meta" in body
        assert len(body["jobs"]) > 0
        assert body["meta"]["total"] >= 49

    async def test_envelope_shape(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert set(body.keys()) == {"jobs", "meta"}
        assert set(body["meta"].keys()) == {
            "total",
            "limit",
            "offset",
            "has_more",
            "applied_filters",
            "applied_sort",
        }

    async def test_per_job_field_set(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        expected_fields = {
            "id",
            "kind",
            "game_id",
            "platform",
            "state",
            "progress",
            "source",
            "started_at",
            "finished_at",
            "error",
            "payload",
        }
        for job in body["jobs"]:
            assert set(job.keys()) == expected_fields


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestJobsPagination:
    async def test_default_limit_50(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["meta"]["limit"] == 50
        assert body["meta"]["offset"] == 0

    async def test_explicit_limit(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["jobs"]) == 10
        assert body["meta"]["limit"] == 10

    async def test_offset_progression(self, client, jobs_pool_seeded):
        r1 = await client.get(
            "/api/v1/jobs?limit=10&offset=0",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.get(
            "/api/v1/jobs?limit=10&offset=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids1 = [j["id"] for j in r1.json()["jobs"]]
        ids2 = [j["id"] for j in r2.json()["jobs"]]
        assert set(ids1).isdisjoint(set(ids2))

    async def test_limit_above_max_returns_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=1000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Filter: enum-valued fields (kind, state, platform, source)
# ---------------------------------------------------------------------------


class TestJobsFilterEnums:
    async def test_kind_eq(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?kind=prefill&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["kind"] == "prefill"

    async def test_kind_in(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?kind_in=prefill,validate&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        kinds = {j["kind"] for j in r.json()["jobs"]}
        assert kinds.issubset({"prefill", "validate"})

    async def test_state_eq_running(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?state=running&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["state"] == "running"

    async def test_state_in_active(self, client, jobs_pool_seeded):
        # Game_shelf "active jobs" canonical query
        r = await client.get(
            "/api/v1/jobs?state_in=queued,running&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        states = {j["state"] for j in r.json()["jobs"]}
        assert states.issubset({"queued", "running"})

    async def test_platform_eq(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?platform=steam&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["platform"] == "steam"

    async def test_source_eq(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?source=cli&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["source"] == "cli"


# ---------------------------------------------------------------------------
# Filter: game_id (FK) + numeric ranges
# ---------------------------------------------------------------------------


class TestJobsFilterScalars:
    async def test_game_id_eq(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?game_id=1&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["game_id"] == 1

    async def test_progress_gte(self, client, jobs_pool_seeded):
        # "Almost done" operator query
        r = await client.get(
            "/api/v1/jobs?progress_gte=0.9&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["progress"] is not None
            assert job["progress"] >= 0.9

    async def test_progress_range(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?progress_gte=0.3&progress_lte=0.7&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert 0.3 <= job["progress"] <= 0.7


# ---------------------------------------------------------------------------
# Filter: timestamp ranges (typed-string validator)
# ---------------------------------------------------------------------------


class TestJobsFilterTimeRange:
    async def test_started_at_gte(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?started_at_gte=2026-05-15&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["started_at"] is not None
            assert job["started_at"] >= "2026-05-15"

    async def test_finished_at_lte(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?finished_at_lte=2026-05-15T00:00:00Z&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["finished_at"] is not None
            assert job["finished_at"] <= "2026-05-15T00:00:00Z"

    async def test_invalid_timestamp_format_returns_400(self, client, jobs_pool_seeded):
        # UAT-4 S3-a regression: XSS-payload as timestamp must be rejected
        r = await client.get(
            "/api/v1/jobs?started_at_gte=<script>alert(1)</script>",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        detail = r.json()["detail"].lower()
        assert "timestamp" in detail or "invalid" in detail


# ---------------------------------------------------------------------------
# Sort (spec D1: id:desc default; tie-breaker dedup)
# ---------------------------------------------------------------------------


class TestJobsSort:
    async def test_default_id_desc(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids = [j["id"] for j in r.json()["jobs"]]
        assert ids == sorted(ids, reverse=True)

    async def test_default_applied_sort_dedup_no_tie_breaker(self, client, jobs_pool_seeded):
        # Spec D1: default sort id:desc means user explicit id wins;
        # tie-breaker id:asc is deduped (UAT-4 S2-B behavior).
        r = await client.get(
            "/api/v1/jobs?limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied == [{"field": "id", "direction": "desc"}]

    async def test_non_id_sort_appends_tie_breaker(self, client, jobs_pool_seeded):
        # User sorts by state; server appends id:asc tie-breaker
        r = await client.get(
            "/api/v1/jobs?sort=state:asc&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied[-1] == {"field": "id", "direction": "asc"}

    async def test_started_at_desc(self, client, jobs_pool_seeded):
        # NULL handling: SQLite default puts NULLs LAST in DESC
        r = await client.get(
            "/api/v1/jobs?sort=started_at:desc&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        starts = [j["started_at"] for j in r.json()["jobs"]]
        non_null = [s for s in starts if s is not None]
        assert non_null == sorted(non_null, reverse=True)


# ---------------------------------------------------------------------------
# Applied echo (UAT-4 S2-A regression: compact plain-dict shape)
# ---------------------------------------------------------------------------


class TestJobsAppliedEcho:
    async def test_applied_filters_compact_dict_shape(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?state=running&kind=prefill&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert applied == {
            "state": {"eq": "running"},
            "kind": {"eq": "prefill"},
        }
        for ops in applied.values():
            assert None not in ops.values()

    async def test_applied_filters_in_op_alias(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?state_in=queued,running",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert "in" in applied["state"]
        assert applied["state"]["in"] == ["queued", "running"]


# ---------------------------------------------------------------------------
# Payload + error
# ---------------------------------------------------------------------------


class TestJobsPayloadAndError:
    async def test_well_formed_payload_parsed(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?state=running&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            if job["payload"] is not None:
                assert isinstance(job["payload"], dict)

    async def test_null_payload(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        any_null = any(j["payload"] is None for j in r.json()["jobs"])
        assert any_null

    async def test_oversized_payload_returns_null(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            if job["payload"] is not None:
                # parsed dict — bounded by cap
                assert len(json.dumps(job["payload"])) < 70000

    async def test_malformed_payload_returns_null(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        corrupt_rows = [
            j for j in r.json()["jobs"] if j["error"] is not None and "json corrupt" in j["error"]
        ]
        assert len(corrupt_rows) == 1
        assert corrupt_rows[0]["payload"] is None

    async def test_non_dict_payload_returns_null(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        candidates = [
            j
            for j in r.json()["jobs"]
            if j["kind"] == "sweep"
            and j["state"] == "succeeded"
            and j["started_at"] == "2026-04-04T00:00:00Z"
        ]
        assert len(candidates) == 1
        assert candidates[0]["payload"] is None

    async def test_error_truncated_to_200(self, client, jobs_pool_seeded, populated_pool):
        # Set one job's error to a long string and re-query
        long_err = "x" * 5000
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE jobs SET error = ? "
                "WHERE id = (SELECT id FROM jobs WHERE state = 'failed' LIMIT 1)",
                (long_err,),
            )
        r = await client.get(
            "/api/v1/jobs?state=failed&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        long_rows = [j for j in r.json()["jobs"] if j["error"] and len(j["error"]) >= 200]
        assert any(len(j["error"]) == 200 for j in long_rows)

    # UAT-5 U5-3: defend against non-string raw_payload pool returns
    async def test_non_buffer_payload_type_returns_null(self, client, populated_pool):
        from unittest.mock import patch

        async def _read_all(*_a, **_kw):
            return [
                {
                    "id": 1,
                    "kind": "prefill",
                    "game_id": None,
                    "platform": "steam",
                    "state": "succeeded",
                    "progress": 1.0,
                    "source": "scheduler",
                    "started_at": "2026-04-04T00:00:00Z",
                    "finished_at": "2026-04-04T00:05:00Z",
                    "error": None,
                    "payload": {"already-decoded": True},  # non-buffer type
                }
            ]

        async def _read_one(*_a, **_kw):
            return {"total": 1}

        with (
            patch("orchestrator.db.pool.Pool.read_all", new=_read_all),
            patch("orchestrator.db.pool.Pool.read_one", new=_read_one),
        ):
            r = await client.get(
                "/api/v1/jobs?limit=500",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 200  # NOT 500
        job = r.json()["jobs"][0]
        assert job["payload"] is None

    # UAT-5 U5-2: Pydantic Literal[] crash on out-of-allow-list DB value
    async def test_out_of_literal_state_skips_row(self, client, populated_pool):
        from unittest.mock import patch

        async def _read_all_garbage_state(*_a, **_kw):
            return [
                {
                    "id": 1,
                    "kind": "prefill",
                    "game_id": None,
                    "platform": "steam",
                    "state": "garbage_value",  # not in Literal
                    "progress": 1.0,
                    "source": "scheduler",
                    "started_at": "2026-04-04T00:00:00Z",
                    "finished_at": "2026-04-04T00:05:00Z",
                    "error": None,
                    "payload": None,
                },
                {
                    "id": 2,
                    "kind": "sweep",
                    "game_id": None,
                    "platform": None,
                    "state": "succeeded",
                    "progress": 1.0,
                    "source": "scheduler",
                    "started_at": "2026-04-04T00:00:00Z",
                    "finished_at": "2026-04-04T00:05:00Z",
                    "error": None,
                    "payload": None,
                },
            ]

        async def _read_one(*_a, **_kw):
            return {"total": 2}

        with (
            patch("orchestrator.db.pool.Pool.read_all", new=_read_all_garbage_state),
            patch("orchestrator.db.pool.Pool.read_one", new=_read_one),
        ):
            r = await client.get(
                "/api/v1/jobs?limit=500",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 200  # NOT 500
        body = r.json()
        assert [j["id"] for j in body["jobs"]] == [2]  # bad row dropped
        assert body["meta"]["total"] == 2


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestJobsErrorPaths:
    async def test_unknown_filter_field_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?password=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "unknown filter field" in r.json()["detail"]

    async def test_unknown_op_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?kind_gte=prefill",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_unknown_sort_field_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?sort=password:desc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_unauth_401(self, client, jobs_pool_seeded):
        r = await client.get("/api/v1/jobs")
        assert r.status_code == 401

    # UAT-5 U5-8: enforce ?include= convention. jobs has no includable keys.
    async def test_unknown_include_key_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?include=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "include" in r.json()["detail"].lower()

    async def test_empty_include_accepted(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?include=&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200  # empty include is a no-op


# ---------------------------------------------------------------------------
# Pool failure (UAT-3+BL6 pattern: structured 503)
# ---------------------------------------------------------------------------


class TestJobsPoolFailure:
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
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}


def test_job_response_accepts_all_db_job_kinds():
    """UAT-9 regression: the /jobs response model dropped manifest_fetch rows
    (its kind Literal was stale), silently hiding them from the API. The
    model's allowed kinds must match the jobs.kind DB CHECK constraint."""
    from orchestrator.api.routers.jobs import JobResponse

    for kind in (
        "prefill",
        "validate",
        "library_sync",
        "auth_refresh",
        "sweep",
        "manifest_fetch",
    ):
        JobResponse(
            id=1,
            kind=kind,
            game_id=None,
            platform="steam",
            state="queued",
            progress=None,
            source="api",
            started_at=None,
            finished_at=None,
            error=None,
            payload=None,
        )
