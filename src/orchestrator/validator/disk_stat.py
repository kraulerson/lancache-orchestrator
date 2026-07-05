"""F7 disk-stat validator engine.

Counts, for a batch of nginx cache file paths, how many are cached.
"Cached" = file exists AND size > 0 (the cache file is larger than the chunk
body because nginx prepends a cache-entry header — never size-match; see spike
A4). We deliberately do NOT require the owner-read bit: a mode-000 cache file
is a TRANSIENT nginx-over-NFS write-race artifact — nginx creates the temp file
at mode 000, then fchmod's it to 0600 milliseconds later (audit 2026-07-02) —
so the content IS on disk at the right size. Penalizing that momentary state
produced false "Partial" badges; the audit confirmed there is no persistent
mode-000 backlog for this relaxation to hide (a genuinely-unreadable file
surfaces separately as an nginx `Permission denied` 500 in the error log).
The cache-key derivation and Steam manifest parsing now live in the data-plane
agent (`agent/routers/steam.py`); this module is the shared stat engine the
agent calls.
"""

from __future__ import annotations

import asyncio
import base64
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

    cached  = a regular file with non-empty size (mode is NOT checked — see
              below).
    present = the file exists on disk (stat succeeds, not a symlink) regardless
              of size/mode — lets callers tell a never-prefilled chunk (absent)
              from an empty one.
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
            # A present, non-empty file is cached. We do NOT require the
            # owner-read bit: a mode-000 file is a TRANSIENT nginx-over-NFS
            # write-race (nginx creates the temp at mode 000, then fchmod's to
            # 0600 ms later; audit 2026-07-02), so the content is on disk. The
            # old read-bit gate produced false "Partial" badges; the audit found
            # no persistent mode-000 backlog for this to hide. (stat() returns
            # st_size without needing read access to the content.)
            if st.st_size > 0:
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


def _purge_batch(paths: list[Path]) -> tuple[int, int, int]:
    """Unlink each present path. Returns (deleted, failed, bytes_freed). Runs in a thread.

    A missing path is a silent no-op (idempotent — re-purge is safe). Bytes are
    counted only for a path that is actually unlinked, so a present-but-undeletable
    file (e.g. a directory, EACCES) counts as ``failed`` and contributes nothing to
    ``bytes_freed``. Never raises — a best-effort failure is expected because the
    re-prefill triggered by the purge is the true safety net (ADR-0015).
    """
    deleted = 0
    failed = 0
    freed = 0
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue  # not present → idempotent no-op, not a failure
        try:
            p.unlink()
        except OSError:
            failed += 1  # present but couldn't remove — best-effort, don't raise
            continue
        deleted += 1
        freed += st.st_size  # only count bytes we actually reclaimed
    return deleted, failed, freed


async def purge_chunks(paths: list[Path], *, batch_size: int = 256) -> tuple[int, int, int]:
    """Delete cache-chunk files. Return (deleted, failed, bytes_freed).

    Mirrors ``validate_chunks``' offload: unlinks run on the same dedicated,
    bounded cache-stat executor (#123.4) in batches, so a large purge never blocks
    the event loop and a hung cache mount can't starve the shared default pool.
    Callers MUST path-validate the list first (agent ``_under_cache_root`` guard);
    this primitive trusts its input and only unlinks what it is given.
    """
    loop = asyncio.get_running_loop()
    executor = _get_cache_stat_executor()
    deleted = 0
    failed = 0
    freed = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        b_deleted, b_failed, b_freed = await loop.run_in_executor(executor, _purge_batch, batch)
        deleted += b_deleted
        failed += b_failed
        freed += b_freed
    if failed:
        _log.warning("purge.unlink_errors", error_count=failed, total=len(paths))
    return deleted, failed, freed


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


def _stat_any_batch(candidate_lists: list[list[Path]]) -> tuple[int, int, int]:
    """Per chunk, cached/present if ANY candidate qualifies. Runs in a thread.

    cached  = at least one candidate is a regular, non-empty file (mode is NOT
              checked — a transient mode-000 file still counts; see _stat_batch).
    present = at least one candidate exists on disk (stat succeeds, not a symlink).
    errors  = unexpected OSErrors across all candidates of all chunks.

    Stops checking a chunk's candidates as soon as `cached` is confirmed (break).
    Symlinks are never genuine cache files and are skipped.
    """
    cached = 0
    present = 0
    errors = 0
    for cands in candidate_lists:
        c_hit = False
        p_hit = False
        for p in cands:
            try:
                if p.is_symlink():
                    continue
                st = p.stat()
                p_hit = True
                if st.st_size > 0:
                    c_hit = True
                    break  # cached wins; stop checking this chunk's candidates
            except FileNotFoundError:
                pass
            except OSError:
                errors += 1
        if c_hit:
            cached += 1
        if p_hit:
            present += 1
    return cached, present, errors


async def validate_chunks_any(
    candidate_lists: list[list[Path]], *, batch_size: int = 256
) -> tuple[int, int]:
    """Return (cached, present) over chunks, each given as a list of candidate
    paths; a chunk counts if ANY candidate qualifies. For Epic, whose content is
    cached under one of several per-CDN-host identifiers. Same bounded executor +
    cached/present rule as validate_chunks_scoped."""
    loop = asyncio.get_running_loop()
    executor = _get_cache_stat_executor()
    cached = 0
    present = 0
    errors = 0
    for i in range(0, len(candidate_lists), batch_size):
        batch = candidate_lists[i : i + batch_size]
        b_cached, b_present, b_err = await loop.run_in_executor(executor, _stat_any_batch, batch)
        cached += b_cached
        present += b_present
        errors += b_err
    if errors:
        _log.warning("validate.stat_errors", error_count=errors, total=len(candidate_lists))
    return cached, present


def _shape(res: dict[str, Any]) -> ValidationResult:
    """Map an agent validate response dict onto a ValidationResult."""
    return ValidationResult(
        chunks_total=res["chunks_total"],
        chunks_cached=res["chunks_cached"],
        chunks_missing=res["chunks_missing"],
        outcome=res["outcome"],
        manifest_version=res.get("versions", ""),
        error=res.get("error"),
    )


async def _validate_epic_game(
    pool: Any, deps: Deps, game_id: int, app_id_str: str
) -> ValidationResult:
    """Validate an Epic game by reading its stored manifest and delegating to the
    agent's /v1/epic/validate endpoint. The manifest's raw bytes are b64-encoded
    for the RPC; cdn_base is required (NULL means a pre-migration row — re-prefill
    heals it by writing cdn_base at prefill time)."""
    manifest = await pool.read_one(
        "SELECT version, cdn_base, raw FROM manifests "
        "WHERE game_id=? ORDER BY fetched_at DESC LIMIT 1",
        (game_id,),
    )
    if manifest is None:
        return ValidationResult(0, 0, 0, "error", "", "no_manifest")
    if not manifest["cdn_base"]:
        return ValidationResult(0, 0, 0, "error", manifest["version"], "no_cdn_base")
    agent = deps.agent_client
    # Narrows Optional[AgentClient] for mypy + guards direct callers of this function.
    if agent is None:
        return ValidationResult(0, 0, 0, "error", "", "agent_client unavailable")
    try:
        app_id_int = int(app_id_str)
    except (TypeError, ValueError):
        app_id_int = 0
    res = await agent.epic_validate(
        app_id=app_id_int,
        version=str(manifest["version"]),
        cdn_base=str(manifest["cdn_base"]),
        raw_manifest_b64=base64.b64encode(manifest["raw"]).decode("ascii"),
    )
    return _shape(res)


async def validate_game(
    pool: Any, deps: Deps, game_id: int, settings: Settings
) -> ValidationResult:
    """Validate a game's current manifests against the on-disk lancache.

    Steam delegates to the agent's `/v1/steam/validate` (SteamPrefill-backed):
    the agent locates the game's manifest .bin files, parses the chunk SHAs,
    derives cache keys, and stats them.

    Epic reads the stored manifest (version + cdn_base + raw bytes) from the DB
    and delegates to the agent's `/v1/epic/validate`.

    The control plane shapes the result; recording is done by validate_one_game.
    """
    if deps.agent_client is None:
        return ValidationResult(0, 0, 0, "error", "", "agent_client unavailable")
    row = await pool.read_one("SELECT app_id, platform FROM games WHERE id=?", (game_id,))
    if row is None:
        return ValidationResult(0, 0, 0, "error", "", f"game {game_id} not found")
    platform = row["platform"]
    if platform == "epic":
        return await _validate_epic_game(pool, deps, game_id, row["app_id"])
    try:
        app_id_int = int(row["app_id"])
    except (TypeError, ValueError):
        return ValidationResult(0, 0, 0, "error", "", "app_id not numeric")
    res = await deps.agent_client.steam_validate(app_id_int)
    return _shape(res)
