"""F7 disk-stat validator engine.

Computes, for each chunk of a game's current depot manifests, the nginx
cache file path and stats it. "Cached" = file exists AND size > 0 (the
cache file is larger than the chunk body because nginx prepends a
cache-entry header — never size-match; see spike A4).

Manifest protobuf parsing happens in the worker venv (ADR-0013 D14) via
`deps.steam_client.manifest_expand`; this module only handles ints, hex
strings, and filesystem paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

# Latest manifest row per depot for a game (max fetched_at, tie-break max id).
_LATEST_PER_DEPOT_SQL = (
    "SELECT m.depot_id AS depot_id, m.version AS version, m.raw AS raw "
    "FROM manifests m "
    "WHERE m.game_id = ? AND m.depot_id IS NOT NULL AND m.id IN ("
    "  SELECT m2.id FROM manifests m2 "
    "  WHERE m2.game_id = m.game_id AND m2.depot_id = m.depot_id "
    "  ORDER BY m2.fetched_at DESC, m2.id DESC LIMIT 1"
    ") ORDER BY m.depot_id"
)


@dataclass
class ValidationResult:
    chunks_total: int
    chunks_cached: int
    chunks_missing: int
    outcome: str  # cached | partial | missing | error
    manifest_version: str
    error: str | None = None


def _stat_batch(paths: list[Path]) -> tuple[int, int]:
    """Count (cached, errors) for a batch. Runs in a thread.

    Cached = a regular file with non-empty size. Symlinks are NOT followed
    (a cache path that is a symlink is not a genuine cached chunk).
    `errors` counts unexpected OSErrors (e.g. EACCES) — a plain missing
    file is not an error.
    """
    cached = 0
    errors = 0
    for p in paths:
        try:
            # A symlink is never a genuine cache file — don't follow it.
            if p.is_symlink():
                continue
            if p.stat().st_size > 0:
                cached += 1
        except FileNotFoundError:
            pass  # plain cache miss — expected, not an error
        except OSError:
            errors += 1  # EACCES, EIO, etc. — surfaced via the run WARN
    return cached, errors


async def validate_chunks(paths: list[Path], *, batch_size: int = 256) -> tuple[int, int]:
    """Return (cached, missing). Cached = a regular, non-empty file.

    Stats are offloaded to the default executor in batches so a large
    chunk list never blocks the event loop. Per-file stat errors (EACCES
    etc.) count as missing and are aggregated into a single WARN per run.
    """
    loop = asyncio.get_running_loop()
    cached = 0
    errors = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        batch_cached, batch_errors = await loop.run_in_executor(None, _stat_batch, batch)
        cached += batch_cached
        errors += batch_errors
    if errors:
        _log.warning("validate.stat_errors", error_count=errors, total=len(paths))
    return cached, len(paths) - cached


def _classify(total: int, cached: int) -> str:
    # total == 0 here means manifests existed but contained no chunks —
    # nothing to cache, so the game is up to date ('cached'). The
    # genuinely-no-manifests case is handled separately (returns 'error'
    # before reaching classification).
    if total == 0:
        return "cached"
    if cached == total:
        return "cached"
    if cached == 0:
        return "missing"
    return "partial"


async def validate_game(
    pool: Any, deps: Deps, game_id: int, settings: Settings
) -> ValidationResult:
    """Validate the latest manifest per depot for `game_id`."""
    cache_root = Path(settings.lancache_nginx_cache_path)
    if not cache_root.is_dir():
        return ValidationResult(0, 0, 0, "error", "", f"cache root not a directory: {cache_root}")

    steam_client = deps.steam_client
    if steam_client is None:
        return ValidationResult(0, 0, 0, "error", "", "steam_client unavailable")

    rows = await pool.read_all(_LATEST_PER_DEPOT_SQL, (game_id,))
    if not rows:
        return ValidationResult(0, 0, 0, "error", "", "no manifests; run manifest fetch first")

    slice_range = slice_range_zero(settings.cache_slice_size_bytes)
    identifier = settings.steam_cache_identifier
    levels = settings.cache_levels

    seen: set[tuple[int, str]] = set()
    paths: list[Path] = []
    versions: list[str] = []
    try:
        for row in rows:
            depot_id = int(row["depot_id"])
            versions.append(f"{depot_id}:{row['version']}")
            expanded = await steam_client.manifest_expand(row["raw"])
            # Bug D: the BLOB must belong to the depot its DB row claims —
            # otherwise we'd stat the wrong CDN paths and report false misses.
            expanded_depot = expanded.get("depot_id")
            if expanded_depot is not None and int(expanded_depot) != depot_id:
                raise ValueError(
                    f"depot_id mismatch: row says {depot_id}, BLOB says {expanded_depot}"
                )
            for sha in expanded.get("chunk_shas", []):
                key = (depot_id, sha)
                if key in seen:
                    continue
                seen.add(key)
                uri = steam_chunk_uri(depot_id, sha)
                h = cache_key(identifier, uri, slice_range)
                paths.append(cache_path(cache_root, h, levels))
    except ValueError as e:
        # Bug C: a malformed chunk SHA / depot mismatch is a data error for
        # this run, not an uncaught crash that loses the whole job.
        _log.warning("validate.expand_error", game_id=game_id, reason=str(e)[:200])
        return ValidationResult(0, 0, 0, "error", ",".join(sorted(versions)), str(e)[:200])

    cached, missing = await validate_chunks(paths)
    total = len(paths)
    outcome = _classify(total, cached)
    _log.info(
        "validate.stat_done",
        game_id=game_id,
        total=total,
        cached=cached,
        missing=missing,
        outcome=outcome,
    )
    return ValidationResult(total, cached, missing, outcome, ",".join(sorted(versions)))
