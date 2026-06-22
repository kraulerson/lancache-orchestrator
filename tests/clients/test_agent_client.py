"""Tests for the control-plane AgentClient."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.clients.agent_client import AgentClient, AgentError

pytestmark = pytest.mark.asyncio

TOKEN = "a" * 32


def _client(handler) -> AgentClient:
    transport = httpx.MockTransport(handler)
    return AgentClient(
        base_url="http://agent:8780",
        token=TOKEN,
        transport=transport,
        poll_interval_sec=0.0,
    )


async def test_pull_posts_then_polls_to_done():
    state = {"polls": 0}

    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.method == "POST" and request.url.path == "/v1/pull":
            return httpx.Response(202, json={"job_id": "j1"})
        if request.url.path == "/v1/pull/j1":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"state": "running", "done": 1, "total": 2})
            return httpx.Response(
                200,
                json={"state": "done", "result": {"chunks_ok": 2, "chunks_failed": 0}},
            )
        raise AssertionError(request.url.path)

    client = _client(handler)
    result = await client.pull([{"url": "/depot/1/chunk/x", "host": "h"}], user_agent="UA/1.0")
    assert result["chunks_ok"] == 2


async def test_steam_prefill_polls_to_done():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "s1"})
        return httpx.Response(200, json={"state": "done", "result": {"ok": True, "raw": "x"}})

    client = _client(handler)
    result = await client.steam_prefill([440], force=False)
    assert result["ok"] is True


async def test_stat_single_call():
    def handler(request):
        assert request.url.path == "/v1/stat"
        return httpx.Response(200, json={"cached": 3, "missing": 1})

    client = _client(handler)
    assert await client.stat(["a" * 32]) == {"cached": 3, "missing": 1}


async def test_auth_status_single_call():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "reason": ""})

    client = _client(handler)
    assert (await client.auth_status())["ok"] is True


async def test_unreachable_raises_agent_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.stat(["a" * 32])


async def test_401_raises_agent_error():
    def handler(request):
        return httpx.Response(401)

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.stat(["a" * 32])


async def test_failed_job_raises_agent_error():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "j"})
        return httpx.Response(200, json={"state": "failed", "error": "boom"})

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.pull([{"url": "/x", "host": "h"}], user_agent="UA/1.0")


async def test_steam_validate_single_call():
    def handler(request):
        assert request.url.path == "/v1/steam/validate"
        return httpx.Response(
            200,
            json={
                "chunks_total": 60,
                "chunks_cached": 60,
                "chunks_missing": 0,
                "outcome": "cached",
                "versions": "1018131:x",
                "error": None,
            },
        )

    client = _client(handler)
    res = await client.steam_validate(1018130)
    assert res["chunks_cached"] == 60
    assert res["outcome"] == "cached"


async def test_steam_validate_unreachable_raises():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.steam_validate(1018130)


async def test_steam_validate_uses_long_timeout():
    # A large validate (tens of thousands of chunks) stat's many files over NFS
    # and can take well over the default 30s; steam_validate must use a generous
    # per-call timeout so it doesn't AgentError on big games.
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            json={
                "chunks_total": 1,
                "chunks_cached": 1,
                "chunks_missing": 0,
                "outcome": "cached",
                "versions": "",
                "error": None,
            },
        )

    client = _client(handler)
    await client.steam_validate(1)
    assert seen["timeout"]["read"] == 300.0
