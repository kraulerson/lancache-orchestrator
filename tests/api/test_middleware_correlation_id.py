"""Tests for CorrelationIdMiddleware (spec §5.2)."""

from __future__ import annotations

import asyncio
import json
import re
import uuid

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class TestCorrelationIdHeaderEcho:
    async def test_cid_generated_when_missing(self, client):
        r = await client.get("/api/v1/health")
        cid = r.headers.get("x-correlation-id")
        assert cid is not None
        assert _UUID4_RE.match(cid)

    async def test_cid_echoed_when_valid(self, client):
        provided = str(uuid.uuid4())
        r = await client.get("/api/v1/health", headers={"X-Correlation-ID": provided})
        assert r.headers.get("x-correlation-id") == provided

    async def test_cid_regenerated_when_invalid(self, client):
        bad = "not-a-uuid"
        r = await client.get("/api/v1/health", headers={"X-Correlation-ID": bad})
        cid = r.headers.get("x-correlation-id")
        assert cid != bad
        assert _UUID4_RE.match(cid)

    async def test_cid_regenerated_when_uuid_v1(self, client):
        v1 = str(uuid.uuid1())
        r = await client.get("/api/v1/health", headers={"X-Correlation-ID": v1})
        cid = r.headers.get("x-correlation-id")
        assert cid != v1
        assert _UUID4_RE.match(cid)


class TestCorrelationIdLogPropagation:
    async def test_cid_appears_in_request_received_log(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        provided = str(uuid.uuid4())
        await client.get("/api/v1/health", headers={"X-Correlation-ID": provided})
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        recv = [e for e in events if e.get("event") == "api.request.received"]
        assert len(recv) >= 1
        assert recv[0].get("correlation_id") == provided

    async def test_cid_appears_in_request_completed_log(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        provided = str(uuid.uuid4())
        await client.get("/api/v1/health", headers={"X-Correlation-ID": provided})
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        comp = [e for e in events if e.get("event") == "api.request.completed"]
        assert len(comp) >= 1
        assert comp[0].get("correlation_id") == provided
        assert "duration_ms" in comp[0]


class TestCorrelationIdIsolation:
    async def test_two_requests_yield_distinct_cids(self, client):
        r1 = await client.get("/api/v1/health")
        r2 = await client.get("/api/v1/health")
        assert r1.headers["x-correlation-id"] != r2.headers["x-correlation-id"]

    async def test_concurrent_requests_have_independent_cids(self, client):
        results = await asyncio.gather(
            client.get("/api/v1/health"),
            client.get("/api/v1/health"),
            client.get("/api/v1/health"),
        )
        cids = [r.headers["x-correlation-id"] for r in results]
        assert len(set(cids)) == 3
