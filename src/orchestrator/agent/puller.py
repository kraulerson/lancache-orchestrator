"""Platform-agnostic chunk puller for the data-plane agent.

Streams each chunk THROUGH the lancache (stream-and-discard) so lancache caches
it. Mirrors prefill/downloader.py's retry/backoff/semaphore loop but takes
explicit (url, host) specs + a per-batch User-Agent, so Steam and Epic collapse
into one puller. `_build_transport()` is the test seam (None -> real network).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)

_BACKOFFS_SEC = (1.0, 4.0, 16.0)
_FAILURE_CAP = 50


@dataclass(frozen=True)
class ChunkSpec:
    url: str  # relative path joined to lancache_base_url
    host: str  # routing Host header (the spoofed CDN host)


@dataclass
class PullResult:
    chunks_total: int
    chunks_ok: int
    chunks_failed: int
    failures: list[tuple[str, str]] = field(default_factory=list)


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject an httpx.MockTransport. None -> real network."""
    return None


def _backoff(attempt: int) -> float:
    return _BACKOFFS_SEC[min(attempt, len(_BACKOFFS_SEC) - 1)]


async def pull_chunks(
    specs: list[ChunkSpec],
    *,
    user_agent: str,
    settings: Settings,
    concurrency: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> PullResult:
    """GET each spec through lancache, streaming + discarding the body."""
    total = len(specs)
    if total == 0:
        return PullResult(0, 0, 0)

    sem = asyncio.Semaphore(concurrency or settings.chunk_concurrency)
    timeout = httpx.Timeout(settings.prefill_chunk_timeout_sec, connect=10.0)
    max_attempts = settings.prefill_chunk_max_attempts

    done = 0
    ok = 0
    failures: list[tuple[str, str]] = []
    lock = asyncio.Lock()

    transport = _build_transport()
    client_kwargs: dict[str, Any] = {
        "base_url": settings.lancache_base_url,
        "timeout": timeout,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def record(url: str, reason: str | None) -> None:
            nonlocal done, ok
            async with lock:
                done += 1
                if reason is None:
                    ok += 1
                else:
                    failures.append((url, reason))
                if on_progress is not None:
                    on_progress(done, total)

        async def fetch(spec: ChunkSpec) -> None:
            headers = {"User-Agent": user_agent, "Host": spec.host}
            reason = "unknown"
            for attempt in range(max_attempts):
                try:
                    async with client.stream("GET", spec.url, headers=headers) as resp:
                        if 200 <= resp.status_code < 300:
                            async for _ in resp.aiter_bytes():
                                pass  # stream + discard
                            await record(spec.url, None)
                            return
                        reason = f"http {resp.status_code}"
                        if resp.status_code < 500:
                            break
                except httpx.RequestError as e:
                    reason = type(e).__name__
                if attempt < max_attempts - 1:
                    await asyncio.sleep(_backoff(attempt))
            await record(spec.url, reason)

        async def guarded(spec: ChunkSpec) -> None:
            async with sem:
                await fetch(spec)

        await asyncio.gather(*(guarded(s) for s in specs))

    return PullResult(
        chunks_total=total,
        chunks_ok=ok,
        chunks_failed=total - ok,
        failures=failures[:_FAILURE_CAP],
    )
