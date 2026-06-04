"""F6: Epic chunk downloader (Host-header routing + cache-HIT verify)."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.core.settings import Settings
from orchestrator.prefill.epic_downloader import (
    EpicPrefillResult,
    prefill_chunks,
    verify_cached,
)

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32


def _settings() -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN)


async def test_prefill_sets_host_and_path(monkeypatch):
    seen: list[tuple[str | None, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.headers.get("host"), req.url.path))
        return httpx.Response(200)

    monkeypatch.setattr(
        "orchestrator.prefill.epic_downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    r = await prefill_chunks(
        ["ChunksV5/00/a_b.chunk"],
        "epiccdn.test",
        "/base",
        _settings(),
        lancache_base_url="http://127.0.0.1",
    )
    assert isinstance(r, EpicPrefillResult)
    assert r.chunks_ok == 1
    assert r.chunks_total == 1
    assert seen[0][0] == "epiccdn.test"
    assert seen[0][1] == "/base/ChunksV5/00/a_b.chunk"


async def test_4xx_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    monkeypatch.setattr(
        "orchestrator.prefill.epic_downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    r = await prefill_chunks(
        ["ChunksV5/00/x.chunk"], "h", "/b", _settings(), lancache_base_url="http://127.0.0.1"
    )
    assert r.chunks_failed == 1
    assert calls["n"] == 1  # 4xx is not retried


async def test_empty_is_noop():
    r = await prefill_chunks([], "h", "/b", _settings(), lancache_base_url="http://127.0.0.1")
    assert r.chunks_total == 0 and r.chunks_ok == 0


async def test_verify_cached_counts_hits(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-Upstream-Cache-Status": "HIT"})

    monkeypatch.setattr(
        "orchestrator.prefill.epic_downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    ratio = await verify_cached(
        ["ChunksV5/00/a.chunk", "ChunksV5/00/b.chunk"],
        "h",
        "/b",
        _settings(),
        lancache_base_url="http://127.0.0.1",
    )
    assert ratio == 1.0


async def test_verify_cached_empty_is_zero():
    ratio = await verify_cached([], "h", "/b", _settings(), lancache_base_url="http://127.0.0.1")
    assert ratio == 0.0
