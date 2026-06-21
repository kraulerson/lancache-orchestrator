"""End-to-end: real agent app + real AgentClient over ASGI transport."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.agent.app import create_agent_app
from orchestrator.clients.agent_client import AgentClient
from orchestrator.core.settings import Settings
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)

pytestmark = pytest.mark.asyncio

TOKEN = "a" * 32


def _agent_client(app) -> AgentClient:
    transport = httpx.ASGITransport(app=app)
    return AgentClient(
        base_url="http://agent",
        token=TOKEN,
        transport=transport,
        poll_interval_sec=0.0,
    )


async def test_e2e_stat(tmp_path):
    # One real cached file + one missing hash, via the real cache-key path.
    slice_range = slice_range_zero(10_485_760)
    sha_present = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"
    sha_absent = "d8e5d44ca8618200552eb754ff6f6922c92a54fe"
    h_present = cache_key("steam", steam_chunk_uri(1, sha_present), slice_range)
    h_absent = cache_key("steam", steam_chunk_uri(1, sha_absent), slice_range)
    p = cache_path(tmp_path, h_present, "2:2")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"data")

    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=tmp_path,
        cache_levels="2:2",
    )
    app = create_agent_app(settings=settings)
    client = _agent_client(app)
    assert await client.stat([h_present, h_absent]) == {"cached": 1, "missing": 1}


async def test_e2e_pull(monkeypatch, tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"x")

    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    settings = Settings(orchestrator_token=TOKEN)
    app = create_agent_app(settings=settings)
    client = _agent_client(app)
    result = await client.pull(
        [{"url": "/depot/1/chunk/x", "host": "lancache.steamcontent.com"}],
        user_agent="UA/1.0",
    )
    assert result["chunks_ok"] == 1
