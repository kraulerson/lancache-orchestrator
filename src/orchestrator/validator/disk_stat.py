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


def _stat_batch(paths: list[Path]) -> int:
    """Count paths that exist with non-empty size. Runs in a thread."""
    cached = 0
    for p in paths:
        try:
            if p.stat().st_size > 0:
                cached += 1
        except OSError:
            pass
    return cached


async def validate_chunks(paths: list[Path], *, batch_size: int = 256) -> tuple[int, int]:
    """Return (cached, missing). Cached = exists AND st_size > 0.

    Stats are offloaded to the default executor in batches so a large
    chunk list never blocks the event loop.
    """
    loop = asyncio.get_running_loop()
    cached = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i : i + batch_size]
        cached += await loop.run_in_executor(None, _stat_batch, batch)
    return cached, len(paths) - cached


def _classify(total: int, cached: int) -> str:
    if total == 0:
        return "error"
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
    for row in rows:
        depot_id = int(row["depot_id"])
        versions.append(f"{depot_id}:{row['version']}")
        expanded = await steam_client.manifest_expand(row["raw"])
        for sha in expanded.get("chunk_shas", []):
            key = (depot_id, sha)
            if key in seen:
                continue
            seen.add(key)
            uri = steam_chunk_uri(depot_id, sha)
            h = cache_key(identifier, uri, slice_range)
            paths.append(cache_path(cache_root, h, levels))

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
