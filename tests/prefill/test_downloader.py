"""Tests for orchestrator.prefill.downloader (F5)."""

from __future__ import annotations

import httpx
import pytest

from orchestrator.core.settings import Settings
from orchestrator.prefill.downloader import prefill_chunks, steam_chunk_download_uri

pytestmark = pytest.mark.asyncio

VALID_TOKEN = "a" * 32
SHA = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"


def _settings(**kw) -> Settings:
    return Settings(orchestrator_token=VALID_TOKEN, **kw)


async def _noop_sleep(_seconds):
    return None


async def test_chunk_uri():
    assert steam_chunk_download_uri(529345, SHA) == f"/depot/529345/chunk/{SHA}"


async def test_all_ok(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"x" * 10)

    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    uris = [f"/depot/1/chunk/{SHA}", f"/depot/1/chunk/{'b' * 40}"]
    result = await prefill_chunks(uris, _settings())
    assert (result.chunks_total, result.chunks_ok, result.chunks_failed) == (2, 2, 0)
    r0 = seen[0]
    assert r0.headers["User-Agent"] == "Valve/Steam HTTP Client 1.0"
    assert r0.headers["Host"] == "lancache.steamcontent.com"
    assert str(r0.url) == f"http://127.0.0.1/depot/1/chunk/{SHA}"


async def test_retry_then_success(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    monkeypatch.setattr("orchestrator.prefill.downloader.asyncio.sleep", _noop_sleep)
    result = await prefill_chunks([f"/depot/1/chunk/{SHA}"], _settings())
    assert (result.chunks_ok, result.chunks_failed) == (1, 0)
    assert calls["n"] == 2  # one retry


async def test_persistent_failure_recorded(monkeypatch):
    def handler(request):
        return httpx.Response(500)

    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    monkeypatch.setattr("orchestrator.prefill.downloader.asyncio.sleep", _noop_sleep)
    result = await prefill_chunks(
        [f"/depot/1/chunk/{SHA}"], _settings(prefill_chunk_max_attempts=2)
    )
    assert (result.chunks_ok, result.chunks_failed) == (0, 1)
    assert result.failures and result.failures[0][0] == f"/depot/1/chunk/{SHA}"


async def test_4xx_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    monkeypatch.setattr("orchestrator.prefill.downloader.asyncio.sleep", _noop_sleep)
    result = await prefill_chunks([f"/depot/1/chunk/{SHA}"], _settings())
    assert result.chunks_failed == 1
    assert calls["n"] == 1  # 4xx not retried


async def test_empty_list(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    result = await prefill_chunks([], _settings())
    assert (result.chunks_total, result.chunks_ok, result.chunks_failed) == (0, 0, 0)


async def test_progress_callback(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(200, content=b"x")),
    )
    seen = []
    uris = [f"/depot/1/chunk/{'a' * 40}", f"/depot/1/chunk/{'b' * 40}"]
    await prefill_chunks(uris, _settings(), on_progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (2, 2)


async def test_decoding_error_records_chunk_failed_not_abort(monkeypatch):
    """A mid-stream DecodingError (corrupt/mislabeled Content-Encoding) must be
    recorded as one failed chunk — not escape gather() and abort the whole run,
    cancelling every sibling chunk download (audit 2026-06-09)."""

    def handler(request):
        if request.url.path.endswith("a" * 40):
            # Content-Encoding: gzip + non-gzip body → DecodingError on stream.
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"not-gzip")
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(
        "orchestrator.prefill.downloader._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    monkeypatch.setattr("orchestrator.prefill.downloader.asyncio.sleep", _noop_sleep)
    uris = [f"/depot/1/chunk/{'a' * 40}", f"/depot/1/chunk/{'b' * 40}"]
    result = await prefill_chunks(uris, _settings(prefill_chunk_max_attempts=1))
    assert result.chunks_total == 2
    assert result.chunks_ok == 1  # the sibling completed (not cancelled)
    assert result.chunks_failed == 1
