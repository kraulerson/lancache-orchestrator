"""Tests for POST /api/v1/new-games-check."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32
H = {"Authorization": f"Bearer {VALID_TOKEN}"}

pytestmark = pytest.mark.asyncio


async def test_trigger_endpoint_queues_with_api_source_and_requires_bearer(client, populated_pool):
    assert (await client.post("/api/v1/new-games-check")).status_code == 401

    r = await client.post("/api/v1/new-games-check", headers=H)
    assert r.status_code == 202
    body = r.json()
    assert body["queued"] is True
    assert isinstance(body["job_id"], int)
    row = await populated_pool.read_one(
        "SELECT kind, source, state FROM jobs WHERE id=?", (body["job_id"],)
    )
    assert row == {"kind": "new_games_check", "source": "api", "state": "queued"}


async def test_trigger_dedup_returns_inflight_job(client):
    r1 = await client.post("/api/v1/new-games-check", headers=H)
    r2 = await client.post("/api/v1/new-games-check", headers=H)
    assert r2.status_code == 202
    assert r2.json()["queued"] is False
    assert r2.json()["job_id"] == r1.json()["job_id"]
