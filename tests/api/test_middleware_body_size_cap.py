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


# Issue #51: Hypothesis property tests for BodySizeCapMiddleware streaming
# path edge cases. Drives the middleware via synthetic ASGI scopes so we
# can control exact chunk timing — httpx-driven tests can't reliably
# reproduce "single mega-chunk" vs "many tiny chunks" patterns.

import pytest  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

_CAP_FOR_PROPERTY = 32 * 1024


def _make_drive_pair(chunks: list[bytes]):
    """Build a (receive, send, response_status_holder) trio that feeds
    `chunks` to the middleware one at a time."""
    queue: list[tuple[bytes, bool]] = [
        (chunk, i < len(chunks) - 1) for i, chunk in enumerate(chunks)
    ]
    status_holder: dict[str, int] = {}

    async def _receive() -> dict:
        if not queue:
            return {"type": "http.request", "body": b"", "more_body": False}
        body, more = queue.pop(0)
        return {"type": "http.request", "body": body, "more_body": more}

    async def _send(msg: dict) -> None:
        if msg["type"] == "http.response.start":
            status_holder["status"] = msg["status"]

    return _receive, _send, status_holder


async def _drive(chunks: list[bytes], cap: int) -> dict[str, int]:
    """Construct a BodySizeCapMiddleware with a real body-reading
    downstream, drive it through `chunks`, return the captured response
    status.

    The downstream does NOT catch `_BodyTooLargeError` — the middleware
    needs to see it propagate to send 413. (Production downstreams in
    FastAPI/Starlette also let it propagate.)"""
    from orchestrator.api.middleware import BodySizeCapMiddleware

    receive, send, status = _make_drive_pair(chunks)

    async def _downstream(scope, recv, snd):
        # Read the body fully — _BodyTooLargeError propagates to middleware
        # for the 413 path; normal completion gets a 200.
        while True:
            msg = await recv()
            if msg["type"] == "http.request" and not msg.get("more_body", False):
                break
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b""})

    mw = BodySizeCapMiddleware(_downstream, cap=cap)
    scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
    await mw(scope, receive, send)
    return status


@settings(
    max_examples=40,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    chunk_sizes=st.lists(
        st.integers(min_value=1, max_value=_CAP_FOR_PROPERTY),
        min_size=1,
        max_size=20,
    )
)
async def test_under_cap_streaming_passes_property(chunk_sizes):
    """Property: any chunking pattern whose TOTAL ≤ cap reaches the
    downstream app (no 413, status=200)."""
    # Constrain total to be under the cap
    total = sum(chunk_sizes)
    if total > _CAP_FOR_PROPERTY:
        ratio = (_CAP_FOR_PROPERTY - 1) / total
        chunk_sizes = [max(1, int(c * ratio)) for c in chunk_sizes]

    chunks = [b"x" * sz for sz in chunk_sizes]
    status = await _drive(chunks, _CAP_FOR_PROPERTY)
    assert status.get("status") == 200, (
        f"under-cap chunks={chunk_sizes} (total={sum(chunk_sizes)}) should pass, got {status}"
    )


@settings(
    max_examples=40,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    chunk_sizes=st.lists(
        st.integers(min_value=1024, max_value=8 * 1024),
        min_size=5,
        max_size=20,
    )
)
async def test_over_cap_streaming_blocked_property(chunk_sizes):
    """Property: chunking patterns whose TOTAL > cap get blocked (413
    or no response — never 200)."""
    total = sum(chunk_sizes)
    if total <= _CAP_FOR_PROPERTY:
        deficit = _CAP_FOR_PROPERTY - total + 1
        chunk_sizes = [*chunk_sizes, deficit]

    chunks = [b"x" * sz for sz in chunk_sizes]
    status = await _drive(chunks, _CAP_FOR_PROPERTY)
    assert status.get("status") in (413, None), (
        f"over-cap chunks={chunk_sizes} (total={sum(chunk_sizes)}) "
        f"should 413 or close, got {status}"
    )


class TestBodySizeCapExactBoundary:
    """Non-property boundary tests for exact-cap and cap+1."""

    @pytest.mark.parametrize("chunk_size", [1, 8, 128, 1024])
    async def test_exact_cap_various_chunk_sizes_pass(self, chunk_size):
        cap = 4096
        n_chunks, remainder = divmod(cap, chunk_size)
        chunks = [b"x" * chunk_size] * n_chunks
        if remainder:
            chunks.append(b"x" * remainder)
        status = await _drive(chunks, cap)
        assert status.get("status") == 200

    async def test_cap_plus_one_blocked(self):
        cap = 4096
        chunks = [b"x" * (cap + 1)]
        status = await _drive(chunks, cap)
        assert status.get("status") == 413
