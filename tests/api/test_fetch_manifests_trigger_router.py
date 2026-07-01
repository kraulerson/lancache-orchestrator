"""Tests for POST /api/v1/fetch-manifests (manifest-fetch trigger)."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32

pytestmark = pytest.mark.asyncio


class TestFetchManifestsTrigger:
    async def test_trigger_requires_bearer(self, client):
        r = await client.post("/api/v1/fetch-manifests")
        assert r.status_code == 401

    async def test_trigger_enqueues(self, client, populated_pool):
        r = await client.post(
            "/api/v1/fetch-manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 202
        body = r.json()
        assert "job_id" in body
        assert isinstance(body["job_id"], int)
        assert body.get("queued") is True
