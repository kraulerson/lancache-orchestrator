"""Tests for BodySizeCapMiddleware (spec §5.3).

Note: body-cap is positioned OUTSIDE bearer-auth in the middleware stack
(spec §5.1). The Content-Length path checks proactively (before any
downstream middleware reads the body). The streaming path only fires
when the app calls receive(); since bearer-auth doesn't read the body,
unauthenticated streaming requests never hit the streaming cap. The
streaming case therefore needs a request that reaches a body-reading
layer — i.e., an authenticated request to a path the cap should still
reject for size.
"""

from __future__ import annotations

import json

VALID_TOKEN = "a" * 32  # matches the conftest dummy token


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
    async def test_chunked_oversize_rejected_413_via_direct_middleware(self):
        """Direct unit test: instantiate BodySizeCapMiddleware with a fake
        downstream app that READS the body, send a fake ASGI streaming
        request that exceeds the cap, verify 413 is sent.

        Why direct: BL5 has no body-consuming endpoint (only GET /health),
        so an HTTP-level streaming-cap test can't trigger the receive()
        path. Direct middleware unit test verifies the streaming check
        works as designed; integration coverage will land when the first
        body-reading endpoint ships in BL6+."""
        from orchestrator.api.middleware import BodySizeCapMiddleware

        downstream_called: list[bool] = []
        sent_messages: list[dict] = []

        async def downstream_app(scope, receive, send):
            # Drain the body — this is what triggers the streaming cap
            while True:
                msg = await receive()
                if not msg.get("more_body", False):
                    break
            # If we got here without _BodyTooLargeError, the cap didn't fire.
            downstream_called.append(True)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = BodySizeCapMiddleware(downstream_app)

        # Build the streaming chunks (no Content-Length → forces streaming path)
        chunks = [b"x" * 1024 for _ in range(33)]  # 33 KiB > 32 KiB cap
        chunks.append(b"")  # final empty chunk signals end

        async def receive():
            if not chunks:
                return {"type": "http.disconnect"}
            chunk = chunks.pop(0)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(chunks),
            }

        async def send(message):
            sent_messages.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/whatever",
            "headers": [],  # no content-length → forces streaming path
        }

        await middleware(scope, receive, send)

        # Downstream should NOT have been called (cap fired first)
        assert not downstream_called, "downstream app reached despite cap"
        # First sent message should be a 413 response
        assert sent_messages[0]["type"] == "http.response.start"
        assert sent_messages[0]["status"] == 413

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
