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


async def test_5xx_retried_then_success(monkeypatch):
    """A transient 503 is retried; a following 200 succeeds (UAT-10 #6)."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200)

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(
        "orchestrator.prefill.epic_downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    monkeypatch.setattr("orchestrator.prefill.epic_downloader.asyncio.sleep", _noop_sleep)
    r = await prefill_chunks(
        ["ChunksV5/00/x.chunk"], "h", "/b", _settings(), lancache_base_url="http://127.0.0.1"
    )
    assert (r.chunks_ok, r.chunks_failed) == (1, 0)
    assert calls["n"] == 2  # one retry, then success


async def test_transport_error_retried_then_failed(monkeypatch):
    """A transport error is retried up to max_attempts, then recorded failed with
    the exception type as the reason (UAT-10 #6)."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(
        "orchestrator.prefill.epic_downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    monkeypatch.setattr("orchestrator.prefill.epic_downloader.asyncio.sleep", _noop_sleep)
    s = Settings(orchestrator_token=VALID_TOKEN, prefill_chunk_max_attempts=2)
    r = await prefill_chunks(
        ["ChunksV5/00/x.chunk"], "h", "/b", s, lancache_base_url="http://127.0.0.1"
    )
    assert r.chunks_failed == 1
    assert calls["n"] == 2  # retried up to max_attempts
    assert r.failures and r.failures[0][1] == "ConnectError"


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


async def test_verify_cached_mixed_hit_miss(monkeypatch):
    """A mix of HIT and non-HIT responses yields the real fraction, exercising
    the MISS branch and the hits/total arithmetic off the 1.0 boundary
    (UAT-10 #8)."""
    statuses = iter(["HIT", "MISS"])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-Upstream-Cache-Status": next(statuses)})

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
    assert ratio == 0.5


async def test_verify_cached_empty_is_zero():
    ratio = await verify_cached([], "h", "/b", _settings(), lancache_base_url="http://127.0.0.1")
    assert ratio == 0.0
