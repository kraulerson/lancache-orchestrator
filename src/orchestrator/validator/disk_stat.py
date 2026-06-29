"""F7 disk-stat validator engine.

Counts, for a batch of nginx cache file paths, how many are cached.
"Cached" = file exists AND size > 0 AND owner-read bit set (the cache file
is larger than the chunk body because nginx prepends a cache-entry header —
never size-match; see spike A4). The cache-key derivation and Steam manifest
parsing now live in the data-plane agent (`agent/routers/steam.py`); this
module is the shared stat engine the agent calls.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from orchestrator.core.settings import Settings
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


@dataclass
class ValidationResult:
    chunks_total: int
    chunks_cached: int
    chunks_missing: int
    outcome: str  # cached | partial | missing | error
    manifest_version: str
    error: str | None = None


# #123.4: a DEDICATED, bounded thread pool for cache stat I/O. The validate
# batch loop awaits each batch sequentially and the jobs worker is serial, so
# one active worker suffices — the bound's purpose is ISOLATION, not parallelism.
# `run_in_executor(None, ...)` would use the shared default pool, which asyncio
# also uses for stdlib offloads like getaddrinfo (DNS). A hung NFS cache mount
# stalling stat() threads would then starve that pool and freeze the
# orchestrator's HTTP probes (lancache heartbeat, Epic API). With a dedicated
# pool, a hung mount can stall at most validation.
_CACHE_STAT_WORKERS = 2
_cache_stat_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _get_cache_stat_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _cache_stat_executor
    if _cache_stat_executor is None:
        _cache_stat_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_CACHE_STAT_WORKERS,
            thread_name_prefix="cache-stat",
        )
    return _cache_stat_executor


def shutdown_cache_stat_executor() -> None:
    """Tear down the dedicated cache-stat pool (idempotent; called from the app
    lifespan shutdown). `wait=False, cancel_futures=True` drops any queued
    batches so a hung-mount backlog can't block shutdown; in-flight stat threads
    can't be force-killed, but normal shutdown won't wait on them."""
    global _cache_stat_executor
    if _cache_stat_executor is not None:
        _cache_stat_executor.shutdown(wait=False, cancel_futures=True)
        _cache_stat_executor = None


def _stat_batch(paths: list[Path]) -> tuple[int, int, int]:
    """Count (cached, present, errors) for a batch. Runs in a thread.

    cached  = a regular file with non-empty size AND the owner-read bit set.
    present = the file exists on disk (stat succeeds, not a symlink) regardless
              of size/mode — lets callers tell a never-prefilled chunk (absent)
              from a prefilled-but-unreadable one (mode-000) or an empty one.
    errors  = unexpected OSErrors (e.g. EACCES on a directory, EIO). A plain
              missing file is not an error.
    """
    cached = 0
    present = 0
    errors = 0
    for p in paths:
        try:
            # A symlink is never a genuine cache file — don't follow it.
            if p.is_symlink():
                continue
            st = p.stat()
            present += 1  # file exists on disk (stat() needs only dir traversal)
            # F5/#128: lancache (www-data, the file owner) must be able to
            # READ the file to serve it. ~1.7% of cache files are mode-000 —
            # they exist with size>0 but are unreadable, so lancache returns
            # 500 + re-downloads. Require the owner-read bit so those don't
            # count as cached. stat() returns st_mode without needing read
            # access to the content, so this works even though the
            # orchestrator (uid 1000) can't open www-data:600 files itself.
            if st.st_size > 0 and (st.st_mode & 0o400):
                cached += 1
        except FileNotFoundError:
            pass  # plain cache miss — expected, not an error
        except OSError:
            errors += 1  # EACCES, EIO, etc. — surfaced via the run WARN
    return cached, present, errors


async def validate_chunks(paths: list[Path], *, batch_size: int = 256) -> tuple[int, int]:
    """Return (cached, missing). Cached = a regular, non-empty file.

    Stats are offloaded to a dedicated, bounded executor (#123.4) in batches so
    a large chunk list never blocks the event loop AND a hung cache mount can't
    starve the shared default pool. Per-file stat errors (EACCES etc.) count as
    missing and are aggregated into a single WARN per run.
    """
    loop = asyncio.get_running_loop()
    executor = _get_cache_stat_executor()
    cached = 0
    errors = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        batch_cached, _batch_present, batch_errors = await loop.run_in_executor(
            executor, _stat_batch, batch
        )
        cached += batch_cached
        errors += batch_errors
    if errors:
        _log.warning("validate.stat_errors", error_count=errors, total=len(paths))
    return cached, len(paths) - cached


async def validate_chunks_scoped(paths: list[Path], *, batch_size: int = 256) -> tuple[int, int]:
    """Return (cached, present) for a chunk list.

    ``present`` counts files that exist on disk regardless of readability, so a
    caller can distinguish a never-prefilled depot (present == 0, all absent)
    from one whose files exist but aren't valid/readable (present > 0 — e.g.
    mode-000 corruption, #76/#128). The latter must stay visible as a real gap,
    NOT be silently dropped as "never prefilled". Same bounded executor + batching
    as `validate_chunks`.
    """
    loop = asyncio.get_running_loop()
    executor = _get_cache_stat_executor()
    cached = 0
    present = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        batch_cached, batch_present, _err = await loop.run_in_executor(executor, _stat_batch, batch)
        cached += batch_cached
        present += batch_present
    return cached, present


async def validate_game(
    pool: Any, deps: Deps, game_id: int, settings: Settings
) -> ValidationResult:
    """Validate a Steam game's current manifests against the on-disk lancache.

    Delegates to the data-plane agent's `/v1/steam/validate`
    (SteamPrefill-backed): the agent locates the game's manifest .bin files,
    parses the chunk SHAs, derives the cache keys, and stats them. The control
    plane only resolves the game's `app_id` and shapes the result.
    """
    if deps.agent_client is None:
        return ValidationResult(0, 0, 0, "error", "", "agent_client unavailable")
    row = await pool.read_one("SELECT app_id FROM games WHERE id=?", (game_id,))
    if row is None:
        return ValidationResult(0, 0, 0, "error", "", f"game {game_id} not found")
    try:
        app_id_int = int(row["app_id"])
    except (TypeError, ValueError):
        return ValidationResult(0, 0, 0, "error", "", "app_id not numeric")
    res = await deps.agent_client.steam_validate(app_id_int)
    return ValidationResult(
        chunks_total=res["chunks_total"],
        chunks_cached=res["chunks_cached"],
        chunks_missing=res["chunks_missing"],
        outcome=res["outcome"],
        manifest_version=res.get("versions", ""),
        error=res.get("error"),
    )
