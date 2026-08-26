"""Uptime Kuma push heartbeats.

UAT-14. Kuma expects a heartbeat per interval and marks a monitor DOWN when none
arrives. The contract from the homelab session: GET <url>?status=up|down&msg=<text>,
url-encoded, 10s timeout.

The governing rule is that monitoring must never be able to break the thing it
monitors. Every failure mode here — no URL configured, DNS gone, connection refused,
timeout, a 500 from Kuma itself — is swallowed. A job's outcome is decided by the job,
never by whether we managed to tell anyone about it.
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator.clients import heartbeat

pytestmark = pytest.mark.asyncio

URL = "http://kuma.example/api/push/abc123"


class _Recorder:
    """Captures the request instead of making one."""

    def __init__(self, *, raise_exc: Exception | None = None, status_code: int = 200):
        self.requests: list[httpx.Request] = []
        self._raise = raise_exc
        self._status = status_code

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._raise is not None:
            raise self._raise
        return httpx.Response(self._status)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


async def test_sends_status_and_message_as_query_parameters() -> None:
    rec = _Recorder()

    await heartbeat.push(URL, status="up", msg="ok", transport=rec.transport)

    assert len(rec.requests) == 1
    sent = rec.requests[0]
    assert sent.method == "GET"
    assert sent.url.params["status"] == "up"
    assert sent.url.params["msg"] == "ok"
    assert str(sent.url).startswith(URL)


async def test_url_encodes_a_message_containing_awkward_characters() -> None:
    rec = _Recorder()
    msg = "prefill failed: 6/359671 chunks failed (http 503) & the agent said 'no'"

    await heartbeat.push(URL, status="down", msg=msg, transport=rec.transport)

    # Round-trips intact rather than truncating at the & or the quote.
    assert rec.requests[0].url.params["msg"] == msg


async def test_no_url_configured_is_a_silent_no_op() -> None:
    """The documented way to disable a heartbeat is to leave the variable unset."""
    rec = _Recorder()

    for empty in (None, "", "   "):
        await heartbeat.push(empty, status="up", msg="ok", transport=rec.transport)

    assert rec.requests == []


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("read timed out"),
    ],
    ids=["refused", "connect_timeout", "read_timeout"],
)
async def test_a_network_failure_never_propagates(failure: Exception) -> None:
    rec = _Recorder(raise_exc=failure)

    # No pytest.raises: the assertion is that this line simply returns.
    await heartbeat.push(URL, status="up", msg="ok", transport=rec.transport)


async def test_an_error_response_never_propagates() -> None:
    rec = _Recorder(status_code=500)

    await heartbeat.push(URL, status="up", msg="ok", transport=rec.transport)

    assert len(rec.requests) == 1, "the request was still attempted"


async def test_a_long_message_is_truncated() -> None:
    """Kuma stores the message; an unbounded error blob does not belong in a URL."""
    rec = _Recorder()

    await heartbeat.push(URL, status="down", msg="x" * 5000, transport=rec.transport)

    sent = rec.requests[0].url.params["msg"]
    assert len(sent) <= heartbeat.MSG_MAX_CHARS
    assert len(sent) >= 100, "truncation must still leave something diagnostic"
