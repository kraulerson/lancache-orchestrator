"""Tests for POST /api/v1/sweep (validate-all trigger, 2026-06-24)."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32

pytestmark = pytest.mark.asyncio


class TestQueueSweep:
    async def test_full_queues_and_returns_202(self, client, populated_pool):
        r = await client.post(
            "/api/v1/sweep",
            json={"full": True},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        body = r.json()
        assert body["full"] is True
        assert isinstance(body["job_id"], int)
        row = await populated_pool.read_one(
            "SELECT kind, payload, source, state FROM jobs WHERE id=?",
            (body["job_id"],),
        )
        assert row == {
            "kind": "sweep",
            "payload": '{"full": true}',
            "source": "api",
            "state": "queued",
        }

    async def test_default_full_false(self, client, populated_pool):
        r = await client.post(
            "/api/v1/sweep",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        body = r.json()
        assert body["full"] is False
        row = await populated_pool.read_one(
            "SELECT payload FROM jobs WHERE id=?", (body["job_id"],)
        )
        assert row["payload"] is None

    async def test_full_after_inflight_default_reports_actual_full(self, client, populated_pool):
        # A fresh full sweep (nothing in flight) queues and reports full=True.
        fresh = await client.post(
            "/api/v1/sweep",
            json={"full": True},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert fresh.status_code == 202
        assert fresh.json()["full"] is True
        assert fresh.json()["queued"] is True

        # Now simulate the misleading case the other way around: a DEFAULT
        # (non-full) sweep is already in flight, then the operator POSTs
        # full=true. The insert is a no-op (ON CONFLICT DO NOTHING), so the
        # response must reflect the EXISTING non-full job — not the request.
        await populated_pool.execute_write(
            "DELETE FROM jobs WHERE kind='sweep' AND state IN ('queued','running')"
        )
        default = await client.post(
            "/api/v1/sweep",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert default.status_code == 202
        assert default.json()["full"] is False
        assert default.json()["queued"] is True
        existing_id = default.json()["job_id"]

        r = await client.post(
            "/api/v1/sweep",
            json={"full": True},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        body = r.json()
        assert body["full"] is False
        assert body["queued"] is False
        assert body["job_id"] == existing_id

    async def test_dedup_returns_existing_inflight(self, client, populated_pool):
        r1 = await client.post(
            "/api/v1/sweep",
            json={"full": True},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.post(
            "/api/v1/sweep",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        # One in-flight sweep allowed (idx_jobs_sweep_inflight); both resolve to it.
        assert r1.json()["job_id"] == r2.json()["job_id"]


class TestErrors:
    async def test_extra_field_rejected(self, client):
        r = await client.post(
            "/api/v1/sweep",
            json={"full": True, "bogus": 1},
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_missing_bearer_returns_401(self, client):
        r = await client.post("/api/v1/sweep", json={"full": True})
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
                "/api/v1/sweep",
                json={"full": True},
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 503
