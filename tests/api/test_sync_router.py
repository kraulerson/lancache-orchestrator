"""Tests for POST /api/v1/platforms/steam/library/sync (BL11)."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32


pytestmark = pytest.mark.asyncio


class TestQueueJob:
    async def test_first_call_queues_job_and_returns_202(self, client, populated_pool):
        r = await client.post(
            "/api/v1/platforms/steam/library/sync",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        body = r.json()
        assert "job_id" in body
        assert isinstance(body["job_id"], int)

        # The job actually lives in the table.
        row = await populated_pool.read_one(
            "SELECT kind, state, platform, source FROM jobs WHERE id=?",
            (body["job_id"],),
        )
        assert row == {
            "kind": "library_sync",
            "state": "queued",
            "platform": "steam",
            "source": "api",
        }


class TestDedup:
    async def test_concurrent_calls_return_same_job_id(self, client, populated_pool):
        first = await client.post(
            "/api/v1/platforms/steam/library/sync",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        second = await client.post(
            "/api/v1/platforms/steam/library/sync",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]

        # Only one queued row exists.
        rows = await populated_pool.read_all(
            "SELECT id FROM jobs WHERE kind='library_sync' AND platform='steam' AND state='queued'"
        )
        assert len(rows) == 1

    async def test_returns_running_job_id_when_in_progress(self, client, populated_pool):
        # Seed a running library_sync job.
        await populated_pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source, started_at) "
            "VALUES (?, ?, 'running', 'api', CURRENT_TIMESTAMP)",
            ("library_sync", "steam"),
        )
        running_id = (
            await populated_pool.read_one(
                "SELECT id FROM jobs WHERE state='running' AND kind='library_sync'"
            )
        )["id"]

        r = await client.post(
            "/api/v1/platforms/steam/library/sync",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        assert r.json()["job_id"] == running_id

    async def test_new_job_returned_after_finished_succeeded(self, client, populated_pool):
        # populated_pool already contains a succeeded library_sync job (id=2).
        succeeded = await populated_pool.read_one(
            "SELECT id FROM jobs WHERE kind='library_sync' AND state='succeeded' "
            "ORDER BY id LIMIT 1"
        )
        assert succeeded is not None, "populated_pool should seed a succeeded job"
        succeeded_id = succeeded["id"]

        r = await client.post(
            "/api/v1/platforms/steam/library/sync",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        new_id = r.json()["job_id"]
        assert new_id != succeeded_id

        row = await populated_pool.read_one("SELECT state FROM jobs WHERE id=?", (new_id,))
        assert row["state"] == "queued"


class TestAuthBoundary:
    async def test_missing_bearer_returns_401(self, client):
        r = await client.post("/api/v1/platforms/steam/library/sync")
        assert r.status_code == 401

    async def test_wrong_bearer_returns_401(self, client):
        r = await client.post(
            "/api/v1/platforms/steam/library/sync",
            headers={"Authorization": "Bearer wrong-token-of-32-characters!!!"},
        )
        assert r.status_code == 401


class TestPoolFailure:
    async def test_db_failure_returns_503(self, unit_app):
        """Override get_pool_dep with a broken pool that raises PoolError."""
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _BrokenPool:
            async def read_one(self, *_a, **_kw):
                raise PoolError("simulated db outage")

            async def execute_write(self, *_a, **_kw):
                raise PoolError("simulated db outage")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _BrokenPool()

        import httpx

        transport = httpx.ASGITransport(app=unit_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            r = await c.post(
                "/api/v1/platforms/steam/library/sync",
                headers={"Authorization": f"Bearer {VALID_TOKEN}"},
            )
        assert r.status_code == 503
