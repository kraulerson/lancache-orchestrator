"""F6 Epic prefill — async chunk downloader.

Downloads Epic CDN chunks THROUGH the lancache (stream-and-discard) so lancache
caches them. Mirrors prefill/downloader.py (Steam) but routes by ``Host`` header
(the CDN host) and prepends the manifest's CDN base path to each chunk path.
See spikes/spike_b_epic_prefill.py.
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
_FAILURE_CAP = 50  # keep retained failure detail bounded


@dataclass
class EpicPrefillResult:
    chunks_total: int
    chunks_ok: int
    chunks_failed: int
    failures: list[tuple[str, str]] = field(default_factory=list)


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject an httpx.MockTransport. None -> real network."""
    return None


def _backoff(attempt: int) -> float:
    return _BACKOFFS_SEC[min(attempt, len(_BACKOFFS_SEC) - 1)]


def _full_path(cdn_base_path: str, chunk_path: str) -> str:
    return f"{cdn_base_path.rstrip('/')}/{chunk_path}"


def _client(cdn_host: str, settings: Settings, lancache_base_url: str | None) -> httpx.AsyncClient:
    timeout = httpx.Timeout(settings.prefill_chunk_timeout_sec, connect=10.0)
    kwargs: dict[str, Any] = {
        "base_url": lancache_base_url or settings.lancache_base_url,
        "timeout": timeout,
        "headers": {"User-Agent": settings.epic_user_agent, "Host": cdn_host},
    }
    transport = _build_transport()
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


async def prefill_chunks(
    chunk_paths: list[str],
    cdn_host: str,
    cdn_base_path: str,
    settings: Settings,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    lancache_base_url: str | None = None,
) -> EpicPrefillResult:
    """GET each Epic chunk through lancache, streaming + discarding the body.

    Bounded by ``Semaphore(chunk_concurrency)``. Each chunk is retried up to
    ``prefill_chunk_max_attempts`` with [1,4,16]s backoff on timeout / transport
    error / 5xx. A 4xx is not retried. "ok" = 2xx.
    """
    total = len(chunk_paths)
    if total == 0:
        return EpicPrefillResult(0, 0, 0)

    sem = asyncio.Semaphore(settings.chunk_concurrency)
    max_attempts = settings.prefill_chunk_max_attempts
    done = 0
    ok = 0
    failures: list[tuple[str, str]] = []
    lock = asyncio.Lock()

    async with _client(cdn_host, settings, lancache_base_url) as client:

        async def record(path: str, reason: str | None) -> None:
            nonlocal done, ok
            async with lock:
                done += 1
                if reason is None:
                    ok += 1
                else:
                    failures.append((path, reason))
                if on_progress is not None:
                    on_progress(done, total)

        async def fetch(chunk_path: str) -> None:
            full = _full_path(cdn_base_path, chunk_path)
            reason = "unknown"
            for attempt in range(max_attempts):
                try:
                    async with client.stream("GET", full) as resp:
                        if 200 <= resp.status_code < 300:
                            async for _ in resp.aiter_bytes():
                                pass  # stream + discard
                            await record(chunk_path, None)
                            return
                        reason = f"http {resp.status_code}"
                        if resp.status_code < 500:
                            break  # 4xx won't be fixed by retry
                except httpx.RequestError as e:
                    # RequestError covers timeouts, transport errors AND
                    # DecodingError (corrupt/mislabeled Content-Encoding from the
                    # Epic CDN via lancache) — record this chunk as failed rather
                    # than aborting the whole run and cancelling sibling chunk
                    # downloads (audit 2026-06-09).
                    reason = type(e).__name__
                if attempt < max_attempts - 1:
                    await asyncio.sleep(_backoff(attempt))
            await record(chunk_path, reason)

        async def guarded(chunk_path: str) -> None:
            async with sem:
                await fetch(chunk_path)

        await asyncio.gather(*(guarded(p) for p in chunk_paths))

    return EpicPrefillResult(
        chunks_total=total,
        chunks_ok=ok,
        chunks_failed=total - ok,
        failures=failures[:_FAILURE_CAP],
    )


async def verify_cached(
    sample_paths: list[str],
    cdn_host: str,
    cdn_base_path: str,
    settings: Settings,
    *,
    lancache_base_url: str | None = None,
) -> float:
    """Re-request a sample of chunks and return the fraction that the lancache
    served from cache (``X-Upstream-Cache-Status: HIT``). 0.0 if the sample is
    empty. Spike-B-proven verification; the disk-stat F7-Epic validator is a
    deferred follow-up."""
    total = len(sample_paths)
    if total == 0:
        return 0.0
    hits = 0
    async with _client(cdn_host, settings, lancache_base_url) as client:
        for chunk_path in sample_paths:
            full = _full_path(cdn_base_path, chunk_path)
            try:
                resp = await client.get(full)
            except (httpx.TimeoutException, httpx.TransportError):
                continue
            if resp.headers.get("X-Upstream-Cache-Status", "").upper() == "HIT":
                hits += 1
    return hits / total
