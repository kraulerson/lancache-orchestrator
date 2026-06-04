"""F6: POST /api/v1/platforms/epic/library/sync."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}

pytestmark = pytest.mark.asyncio


async def test_first_call_queues_job_and_returns_202(client, populated_pool):
    r = await client.post("/api/v1/platforms/epic/library/sync", headers=AUTH)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert isinstance(job_id, int)
    row = await populated_pool.read_one(
        "SELECT kind, platform, state, source FROM jobs WHERE id=?", (job_id,)
    )
    assert row == {
        "kind": "library_sync",
        "platform": "epic",
        "state": "queued",
        "source": "api",
    }


async def test_dedup_returns_same_job_id(client, populated_pool):
    first = await client.post("/api/v1/platforms/epic/library/sync", headers=AUTH)
    second = await client.post("/api/v1/platforms/epic/library/sync", headers=AUTH)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    rows = await populated_pool.read_all(
        "SELECT id FROM jobs WHERE kind='library_sync' AND platform='epic' AND state='queued'"
    )
    assert len(rows) == 1


async def test_epic_sync_independent_of_steam(client, populated_pool):
    # An in-flight steam library_sync must not block an epic one (per-platform).
    await populated_pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) "
        "VALUES ('library_sync', 'steam', 'queued', 'api')"
    )
    r = await client.post("/api/v1/platforms/epic/library/sync", headers=AUTH)
    assert r.status_code == 202


async def test_missing_bearer_returns_401(client):
    r = await client.post("/api/v1/platforms/epic/library/sync")
    assert r.status_code == 401
