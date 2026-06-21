"""Tests for the agent's platform-agnostic chunk puller."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.agent.puller import ChunkSpec, pull_chunks
from orchestrator.core.settings import Settings

pytestmark = pytest.mark.asyncio

SHA = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"


def _settings(**kw) -> Settings:
    return Settings(orchestrator_token="a" * 32, **kw)


async def test_all_ok_sets_host_and_ua_per_request(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"x" * 10)

    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    specs = [
        ChunkSpec(url=f"/depot/1/chunk/{SHA}", host="lancache.steamcontent.com"),
        ChunkSpec(url="/Builds/x/chunk0", host="epicgames-download1.akamaized.net"),
    ]
    result = await pull_chunks(specs, user_agent="UA/1.0", settings=_settings())
    assert (result.chunks_total, result.chunks_ok, result.chunks_failed) == (2, 2, 0)
    assert seen[0].headers["User-Agent"] == "UA/1.0"
    assert seen[0].headers["Host"] == "lancache.steamcontent.com"
    assert seen[1].headers["Host"] == "epicgames-download1.akamaized.net"
    assert str(seen[0].url) == f"http://127.0.0.1/depot/1/chunk/{SHA}"


async def test_4xx_not_retried_recorded(monkeypatch):
    def handler(request):
        return httpx.Response(404)

    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    specs = [ChunkSpec(url="/depot/1/chunk/x", host="h")]
    result = await pull_chunks(specs, user_agent="UA/1.0", settings=_settings())
    assert (result.chunks_ok, result.chunks_failed) == (0, 1)
    assert result.failures == [("/depot/1/chunk/x", "http 404")]


async def test_empty_is_zero():
    result = await pull_chunks([], user_agent="UA/1.0", settings=_settings())
    assert (result.chunks_total, result.chunks_ok, result.chunks_failed) == (0, 0, 0)


async def test_progress_callback(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x")

    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    seen_progress = []
    specs = [ChunkSpec(url=f"/c/{i}", host="h") for i in range(3)]
    await pull_chunks(
        specs,
        user_agent="UA/1.0",
        settings=_settings(),
        on_progress=lambda d, t: seen_progress.append((d, t)),
    )
    assert seen_progress[-1] == (3, 3)
