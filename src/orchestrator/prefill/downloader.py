"""F5 Steam prefill — async chunk downloader.

Downloads depot chunks THROUGH the lancache (stream-and-discard) so lancache
caches them under the key F7 validates. See ``spikes/spike_a5_prefill.md``.

Runs in the orchestrator process (httpx async); no steam-next/worker is needed
for the download itself — the chunk URLs are unauthenticated content paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from orchestrator.validator.cache_key import steam_chunk_uri

if TYPE_CHECKING:
    from collections.abc import Callable

    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)

_BACKOFFS_SEC = (1.0, 4.0, 16.0)
_FAILURE_CAP = 50  # keep retained failure detail bounded


def steam_chunk_download_uri(depot_id: int, sha_hex: str) -> str:
    """``/depot/{depot_id}/chunk/{sha}`` — reuses the validator shape checks."""
    return steam_chunk_uri(depot_id, sha_hex)


@dataclass
class PrefillResult:
    chunks_total: int
    chunks_ok: int
    chunks_failed: int
    failures: list[tuple[str, str]] = field(default_factory=list)


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject an ``httpx.MockTransport``. None → real network."""
    return None


def _backoff(attempt: int) -> float:
    return _BACKOFFS_SEC[min(attempt, len(_BACKOFFS_SEC) - 1)]


async def prefill_chunks(
    chunk_uris: list[str],
    settings: Settings,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> PrefillResult:
    """GET each chunk URI through lancache, streaming + discarding the body.

    Bounded by ``Semaphore(chunk_concurrency)``. Each chunk is retried up to
    ``prefill_chunk_max_attempts`` with [1,4,16]s backoff on timeout /
    transport error / 5xx. A 4xx is not retried. "ok" = 2xx.
    """
    total = len(chunk_uris)
    if total == 0:
        return PrefillResult(0, 0, 0)

    sem = asyncio.Semaphore(settings.chunk_concurrency)
    headers = {
        "User-Agent": settings.prefill_user_agent,
        "Host": settings.steam_cdn_host,
    }
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

        async def record(uri: str, reason: str | None) -> None:
            nonlocal done, ok
            async with lock:
                done += 1
                if reason is None:
                    ok += 1
                else:
                    failures.append((uri, reason))
                if on_progress is not None:
                    on_progress(done, total)

        async def fetch(uri: str) -> None:
            reason = "unknown"
            for attempt in range(max_attempts):
                try:
                    async with client.stream("GET", uri, headers=headers) as resp:
                        if 200 <= resp.status_code < 300:
                            async for _ in resp.aiter_bytes():
                                pass  # stream + discard
                            await record(uri, None)
                            return
                        reason = f"http {resp.status_code}"
                        if resp.status_code < 500:
                            break  # 4xx won't be fixed by retry
                except httpx.RequestError as e:
                    # RequestError covers timeouts, transport errors AND
                    # DecodingError (corrupt/mislabeled Content-Encoding from
                    # lancache/upstream) — record this chunk as failed rather
                    # than letting it escape gather() and abort the whole run,
                    # cancelling every sibling chunk download (audit 2026-06-09).
                    reason = type(e).__name__
                if attempt < max_attempts - 1:
                    await asyncio.sleep(_backoff(attempt))
            await record(uri, reason)

        async def guarded(uri: str) -> None:
            async with sem:
                await fetch(uri)

        await asyncio.gather(*(guarded(u) for u in chunk_uris))

    return PrefillResult(
        chunks_total=total,
        chunks_ok=ok,
        chunks_failed=total - ok,
        failures=failures[:_FAILURE_CAP],
    )
