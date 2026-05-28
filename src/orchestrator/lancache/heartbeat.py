"""Lancache heartbeat probe — ID2 self-test (FRD §5 + Bible §8.4).

GET /lancache-heartbeat against the operator-configured lancache base
URL. The probe is async, time-bounded (httpx per-call timeout), result-
cached with a TTL to avoid hammering lancache on every /health call,
and concurrency-safe (parallel callers collapse onto one in-flight
request via an asyncio.Lock).

Failure modes are treated uniformly as "not reachable" — connect
timeout, read timeout, connect error, any non-200 status, even
unexpected exceptions. The /health surface only needs a boolean.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

_log = structlog.get_logger(__name__)

# Lancache nginx identifies itself with this header on EVERY response,
# including the /lancache-heartbeat 204. Verified against lancache.net's
# `lancachenet/monolithic` image at v3.x. We require the header (not
# just any 2xx) so a misconfigured DNS bypass pointing at a different
# server can't accidentally look "reachable". Surfaced by post-PR-#113
# deployment testing where the bare-2xx check passed against a real
# lancache returning 204 but would have also passed against any other
# 2xx-responding service.
LANCACHE_IDENTIFIER_HEADER = "X-LanCache-Processed-By"


class LancacheProbe:
    """Cached async HTTP probe of `GET <url>` for lancache reachability.

    Lifecycle:
        - Construct once at FastAPI lifespan startup
        - `probe()` is awaitable; safe to call concurrently
        - `last_result()` reads cached value without triggering a call
        - `invalidate()` forces the next `probe()` to refresh
    """

    def __init__(
        self,
        *,
        url: str,
        timeout_sec: float = 5.0,
        cache_ttl_sec: float = 30.0,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if not url:
            raise ValueError("url must be non-empty")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://; got {url!r}")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        if cache_ttl_sec < 0:
            raise ValueError("cache_ttl_sec must be non-negative")

        self._url = url
        self._timeout_sec = timeout_sec
        self._cache_ttl_sec = cache_ttl_sec
        self._monotonic = monotonic_fn

        self._last_result: bool = False
        self._last_checked_at_mono: float | None = None
        # Single lock collapses concurrent probes onto one in-flight call.
        self._refresh_lock = asyncio.Lock()

    def last_result(self) -> bool:
        return self._last_result

    def last_checked_at_mono(self) -> float | None:
        return self._last_checked_at_mono

    def invalidate(self) -> None:
        """Force the next probe() to ignore the TTL and refresh."""
        self._last_checked_at_mono = None

    def _cache_fresh(self) -> bool:
        if self._last_checked_at_mono is None:
            return False
        age = self._monotonic() - self._last_checked_at_mono
        return age < self._cache_ttl_sec

    async def probe(self) -> bool:
        """Refresh the cached probe result if stale, return the (now-)current
        value. Never raises — all failure modes return False."""
        # Fast path: TTL still valid, return cached value without any IO
        # or lock contention.
        if self._cache_fresh():
            return self._last_result

        # Slow path: serialize the refresh so 10 concurrent callers don't
        # generate 10 outbound HTTP calls. Inside the lock we re-check
        # freshness; the first caller that arrives does the work, the
        # rest find a fresh cache and skip.
        async with self._refresh_lock:
            if self._cache_fresh():
                return self._last_result
            await self._refresh()
        return self._last_result

    async def _refresh(self) -> None:
        ok = False
        try:
            async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                response = await client.get(self._url)
                # Lancache nginx returns 204 No Content on the heartbeat
                # endpoint (verified against the running lancachenet/
                # monolithic image). Accept any 2xx and require the
                # `X-LanCache-Processed-By` header so the probe doesn't
                # call a non-lancache service "reachable" by accident.
                ok = (
                    200 <= response.status_code < 300
                    and LANCACHE_IDENTIFIER_HEADER in response.headers
                )
                if not ok and 200 <= response.status_code < 300:
                    _log.warning(
                        "lancache.probe.missing_identifier_header",
                        url=self._url,
                        status_code=response.status_code,
                        header=LANCACHE_IDENTIFIER_HEADER,
                    )
        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.TimeoutException,
        ) as e:
            _log.warning(
                "lancache.probe.network_error",
                url=self._url,
                error=type(e).__name__,
                reason=str(e)[:200],
            )
        except Exception as e:
            # Defensive: any other exception (DNS pathology, mis-typed
            # response, library bug) must NOT crash /health.
            _log.error(
                "lancache.probe.unexpected_error",
                url=self._url,
                error=type(e).__name__,
                reason=str(e)[:200],
            )

        previous = self._last_result
        self._last_result = ok
        self._last_checked_at_mono = self._monotonic()
        if ok != previous:
            _log.info(
                "lancache.probe.state_changed",
                url=self._url,
                reachable=ok,
                previous=previous,
            )
