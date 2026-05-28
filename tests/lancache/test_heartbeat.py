"""Tests for orchestrator.lancache.heartbeat — ID2 lancache self-test probe."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from orchestrator.lancache.heartbeat import LancacheProbe

pytestmark = pytest.mark.asyncio


URL = "http://lancache:80/lancache-heartbeat"

# Lancache nginx stamps this header on every response, including the
# 204 returned by /lancache-heartbeat. Verified against the running
# `lancachenet/monolithic` image. The probe requires it as positive
# identification.
LANCACHE_HEADERS = {"X-LanCache-Processed-By": "test-lancache-host"}


def _ok_response(
    text: str = "",
    status: int = 204,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Mock a healthy lancache heartbeat response — 204 No Content with
    the identifier header (default), matching real lancache behavior."""
    return httpx.Response(
        status_code=status,
        text=text,
        request=httpx.Request("GET", URL),
        headers=headers if headers is not None else LANCACHE_HEADERS,
    )


def _status_response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text="",
        request=httpx.Request("GET", URL),
        headers=headers or {},
    )


class TestProbeBasics:
    async def test_initial_state_unreachable(self):
        """Before any probe runs, `reachable` must report False — we cannot
        claim a lancache is reachable when we've never asked."""
        probe = LancacheProbe(url=URL)
        assert probe.last_result() is False
        assert probe.last_checked_at_mono() is None

    async def test_success_returns_true_and_caches(self):
        probe = LancacheProbe(url=URL, cache_ttl_sec=30.0)
        mock = AsyncMock(return_value=_ok_response())
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            result = await probe.probe()
        assert result is True
        assert probe.last_result() is True
        assert probe.last_checked_at_mono() is not None
        assert mock.call_count == 1

    async def test_non_2xx_returns_false(self):
        probe = LancacheProbe(url=URL)
        for status in (301, 302, 400, 401, 403, 404, 500, 502, 503):
            mock = AsyncMock(return_value=_status_response(status, LANCACHE_HEADERS))
            with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
                # Fresh probe each iteration so caching doesn't suppress the call.
                probe = LancacheProbe(url=URL, cache_ttl_sec=0.0)
                result = await probe.probe()
            assert result is False, f"status={status} should report unreachable"

    async def test_accepts_204_with_identifier_header(self):
        """Real lancache returns 204 No Content + X-LanCache-Processed-By
        header — verified against a running lancachenet/monolithic image."""
        probe = LancacheProbe(url=URL)
        mock = AsyncMock(return_value=_ok_response(status=204))
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            assert await probe.probe() is True

    async def test_accepts_200_with_identifier_header(self):
        """Some lancache variants return 200; still healthy as long as the
        identifier header is present."""
        probe = LancacheProbe(url=URL)
        mock = AsyncMock(return_value=_ok_response(status=200))
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            assert await probe.probe() is True

    async def test_accepts_all_2xx_with_identifier_header(self):
        probe = LancacheProbe(url=URL, cache_ttl_sec=0.0)
        for status in (200, 201, 202, 204, 206):
            mock = AsyncMock(return_value=_ok_response(status=status))
            with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
                assert await probe.probe() is True, f"status={status} should be reachable"

    async def test_2xx_without_identifier_header_returns_false(self):
        """Defends against a misconfigured DNS bypass / wrong target —
        a generic 2xx-responding service is not lancache."""
        probe = LancacheProbe(url=URL)
        mock = AsyncMock(return_value=_status_response(204, headers={}))
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            assert await probe.probe() is False

    async def test_header_match_is_case_insensitive(self):
        """httpx normalizes header lookup; verify the probe handles
        lower-case + mixed-case header names."""
        probe = LancacheProbe(url=URL, cache_ttl_sec=0.0)
        for header_name in (
            "X-LanCache-Processed-By",
            "x-lancache-processed-by",
            "X-LANCACHE-PROCESSED-BY",
        ):
            mock = AsyncMock(return_value=_status_response(204, headers={header_name: "host"}))
            with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
                assert await probe.probe() is True, f"header={header_name!r}"


class TestErrorHandling:
    async def test_connect_timeout_returns_false(self):
        probe = LancacheProbe(url=URL)
        mock = AsyncMock(side_effect=httpx.ConnectTimeout("simulated"))
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            assert await probe.probe() is False
        assert probe.last_result() is False

    async def test_read_timeout_returns_false(self):
        probe = LancacheProbe(url=URL)
        mock = AsyncMock(side_effect=httpx.ReadTimeout("simulated"))
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            assert await probe.probe() is False

    async def test_connect_error_returns_false(self):
        probe = LancacheProbe(url=URL)
        mock = AsyncMock(side_effect=httpx.ConnectError("dns/conn failure"))
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            assert await probe.probe() is False

    async def test_unexpected_exception_returns_false(self):
        """Defensive: any other exception during probe must NOT crash /health."""
        probe = LancacheProbe(url=URL)
        mock = AsyncMock(side_effect=RuntimeError("something weird"))
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            assert await probe.probe() is False


class TestCaching:
    async def test_within_ttl_returns_cached_result_without_call(self):
        probe = LancacheProbe(url=URL, cache_ttl_sec=30.0)
        mock = AsyncMock(return_value=_ok_response())
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            await probe.probe()  # primes the cache
            await probe.probe()
            await probe.probe()
        assert mock.call_count == 1  # only the first call hit httpx

    async def test_ttl_zero_disables_cache(self):
        probe = LancacheProbe(url=URL, cache_ttl_sec=0.0)
        mock = AsyncMock(return_value=_ok_response())
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            await probe.probe()
            await probe.probe()
            await probe.probe()
        assert mock.call_count == 3

    async def test_expired_ttl_triggers_refresh(self):
        """Drive monotonic clock manually to advance past TTL."""
        clock = [1000.0]
        probe = LancacheProbe(
            url=URL,
            cache_ttl_sec=30.0,
            monotonic_fn=lambda: clock[0],
        )
        mock = AsyncMock(return_value=_ok_response())
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            await probe.probe()
            assert mock.call_count == 1

            clock[0] += 20.0  # within TTL
            await probe.probe()
            assert mock.call_count == 1

            clock[0] += 15.0  # 35s elapsed total — past 30s TTL
            await probe.probe()
            assert mock.call_count == 2

    async def test_invalidate_forces_refresh(self):
        probe = LancacheProbe(url=URL, cache_ttl_sec=30.0)
        mock = AsyncMock(return_value=_ok_response())
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            await probe.probe()
            probe.invalidate()
            await probe.probe()
        assert mock.call_count == 2


class TestConcurrency:
    async def test_concurrent_probes_collapse_to_single_call(self):
        """If 10 concurrent /health requests trigger a probe at once,
        only ONE outbound HTTP call should fire — the rest wait on the
        in-flight result. Prevents thundering herd against lancache."""
        probe = LancacheProbe(url=URL, cache_ttl_sec=30.0)
        gate = asyncio.Event()

        async def slow_get(*_args, **_kwargs):
            await gate.wait()
            return _ok_response()

        with patch.object(
            httpx.AsyncClient, "get", new=AsyncMock(side_effect=slow_get), create=True
        ) as mock:
            tasks = [asyncio.create_task(probe.probe()) for _ in range(10)]
            await asyncio.sleep(0.05)
            gate.set()
            results = await asyncio.gather(*tasks)
            assert all(r is True for r in results)
            assert mock.call_count == 1


class TestLastCheckedAt:
    async def test_last_checked_at_advances_on_refresh(self):
        clock = [500.0]
        probe = LancacheProbe(
            url=URL,
            cache_ttl_sec=10.0,
            monotonic_fn=lambda: clock[0],
        )
        mock = AsyncMock(return_value=_ok_response())
        with patch.object(httpx.AsyncClient, "get", new=mock, create=True):
            await probe.probe()
            first = probe.last_checked_at_mono()
            assert first == 500.0

            clock[0] = 600.0  # past 10s TTL
            await probe.probe()
            second = probe.last_checked_at_mono()
            assert second == 600.0
            assert second > first


class TestUrlValidation:
    async def test_rejects_empty_url(self):
        with pytest.raises(ValueError, match="url must be non-empty"):
            LancacheProbe(url="")

    async def test_rejects_non_http_url(self):
        """We're probing a known plain-HTTP endpoint inside the LAN — refuse
        schemes that would either fail or introduce TLS surprises."""
        with pytest.raises(ValueError, match="url must start with http"):
            LancacheProbe(url="ftp://lancache/lancache-heartbeat")

    async def test_accepts_http_and_https(self):
        LancacheProbe(url="http://lancache:80/lancache-heartbeat")
        LancacheProbe(url="https://lancache.example.com/lancache-heartbeat")
