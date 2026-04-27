"""Tests for BodySizeCapMiddleware (spec §5.3)."""

from __future__ import annotations

import json


class TestBodySizeCapContentLength:
    async def test_oversize_content_length_rejected_413(self, client):
        body = b"x" * (32 * 1024 + 1)
        r = await client.post(
            "/api/v1/anything",
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 413

    async def test_at_cap_content_length_passes_to_handler(self, client):
        body = b"x" * (32 * 1024)
        r = await client.post("/api/v1/health", content=body)
        # /health is exempt from auth; POST routes to a 405 — that's fine,
        # what we want is to verify body cap didn't fire (no 413).
        assert r.status_code != 413

    async def test_under_cap_content_length_passes(self, client):
        body = b"x" * 100
        r = await client.post("/api/v1/health", content=body)
        assert r.status_code != 413


class TestBodySizeCapStreaming:
    async def test_chunked_oversize_rejected_413(self, client):
        async def gen():
            for _ in range(33):  # 33 KiB total > 32 KiB cap
                yield b"x" * 1024

        r = await client.post(
            "/api/v1/anything",
            content=gen(),
            headers={"Transfer-Encoding": "chunked"},
        )
        assert r.status_code == 413

    async def test_chunked_under_cap_passes(self, client):
        async def gen():
            for _ in range(10):  # 10 KiB total
                yield b"x" * 1024

        r = await client.post(
            "/api/v1/health",
            content=gen(),
            headers={"Transfer-Encoding": "chunked"},
        )
        assert r.status_code != 413


class TestBodySizeCapLogging:
    async def test_413_emits_structured_event(self, client, capsys):
        from orchestrator.core.logging import configure_logging

        configure_logging()
        body = b"x" * (32 * 1024 + 1)
        await client.post("/api/v1/anything", content=body)
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        names = [e.get("event") for e in events]
        assert "api.body_size_cap_exceeded" in names
