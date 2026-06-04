"""F5 — prefill job handler.

Builds the game's deduped chunk list (latest manifest per depot →
``manifest.expand``, reusing F7's query) and downloads each chunk through the
lancache so it gets cached. On full success, enqueues a ``validate`` job (ID5),
which sets the game's final status.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.platform.epic.manifest import chunk_path as epic_chunk_path
from orchestrator.platform.epic.models import EpicLibraryItem
from orchestrator.prefill.downloader import prefill_chunks, steam_chunk_download_uri
from orchestrator.prefill.epic_downloader import prefill_chunks as epic_prefill_chunks
from orchestrator.prefill.epic_downloader import verify_cached as epic_verify_cached
from orchestrator.validator.disk_stat import _LATEST_PER_DEPOT_SQL

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps
    from orchestrator.platform.epic.client import EpicClient

_log = structlog.get_logger(__name__)

# Epic manifest upsert (depot_id is NULL — Epic has no depots). Keyed on
# (game_id, version); a re-fetch updates the existing row.
_EPIC_MANIFEST_UPSERT = (
    "INSERT INTO manifests (game_id, depot_id, version, fetched_at, chunk_count, total_bytes, raw) "
    "VALUES (?, NULL, ?, CURRENT_TIMESTAMP, ?, ?, ?) "
    "ON CONFLICT(game_id, version) DO UPDATE SET "
    "  fetched_at = CURRENT_TIMESTAMP, "
    "  chunk_count = excluded.chunk_count, "
    "  total_bytes = excluded.total_bytes, "
    "  raw = excluded.raw"
)


async def _load_chunk_uris(deps: Deps, game_id: int) -> list[str]:
    """Deduped /depot/{id}/chunk/{sha} URIs from the latest manifest per depot."""
    client = deps.steam_client
    if client is None:
        raise RuntimeError("steam_client is required for prefill handler")
    rows = await deps.pool.read_all(_LATEST_PER_DEPOT_SQL, (game_id,))
    seen: set[tuple[int, str]] = set()
    uris: list[str] = []
    for row in rows:
        depot_id = int(row["depot_id"])
        expanded = await client.manifest_expand(row["raw"])
        for sha in expanded.get("chunk_shas", []):
            key = (depot_id, sha)
            if key in seen:
                continue
            seen.add(key)
            uris.append(steam_chunk_download_uri(depot_id, sha))
    return uris


async def prefill_handler(job: dict[str, Any], deps: Deps) -> None:
    """Prefill one game's chunks through the lancache — dispatches on platform."""
    platform = job.get("platform")
    if platform == "steam":
        return await _steam_prefill(job, deps)
    if platform == "epic":
        return await _epic_prefill(job, deps)
    raise ValueError(f"prefill: unsupported platform {platform!r}")


async def _epic_prefill(job: dict[str, Any], deps: Deps) -> None:
    """Prefill one Epic game (F6): set downloading → fetch a FRESH manifest
    (Epic signed URLs expire) → store it → download chunks through the lancache →
    sample-verify the cache HIT → mark up_to_date. F7-Epic disk-stat validation is
    a deferred follow-up, so the inline HIT verification is the validation here."""
    if deps.epic_client is None:
        raise RuntimeError("epic_client is required for epic prefill handler")
    epic_client = deps.epic_client
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("prefill job has no game_id")
    game = await deps.pool.read_one(
        "SELECT id, app_id, title, platform, metadata FROM games WHERE id=?", (game_id,)
    )
    if game is None:
        raise ValueError(f"game {game_id} not found in games table")
    if game["platform"] != "epic":
        raise ValueError(f"game {game_id} platform is {game['platform']!r}, not epic")

    job_id = job.get("id")
    await deps.pool.execute_write("UPDATE games SET status='downloading' WHERE id=?", (game_id,))
    _log.info("prefill.epic.started", job_id=job_id, game_id=game_id)
    try:
        await _epic_prefill_inner(job_id, game_id, game, deps, epic_client)
    except Exception as e:
        # Never leave the game stuck in 'downloading'. The chunk-failure path
        # already set 'failed' (the guard then no-ops); any other failure
        # (auth/manifest/network) is marked here before the re-raise.
        with contextlib.suppress(Exception):
            await deps.pool.execute_write(
                "UPDATE games SET status='failed', last_error=? "
                "WHERE id=? AND status='downloading'",
                (f"prefill: {type(e).__name__}: {e}"[:200], game_id),
            )
        raise


async def _epic_prefill_inner(
    job_id: Any,
    game_id: int,
    game: dict[str, Any],
    deps: Deps,
    epic_client: EpicClient,
) -> None:
    try:
        meta = json.loads(game["metadata"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    item = EpicLibraryItem(
        app_name=str(game["app_id"]),
        namespace=str(meta.get("namespace", "")),
        catalog_item_id=str(meta.get("catalog_item_id", "")),
        title=str(game["title"] or game["app_id"]),
    )
    # FRESH fetch — never reuse a stored signed manifest/CDN URL (they expire).
    manifest, cdn_host, cdn_base = await epic_client.fetch_manifest(item)

    total_bytes = sum(c.file_size for c in manifest.chunks)
    await deps.pool.execute_write(
        _EPIC_MANIFEST_UPSERT,
        (game_id, str(manifest.version), len(manifest.chunks), total_bytes, manifest.raw),
    )
    await deps.pool.execute_write(
        "UPDATE games SET size_bytes=? WHERE id=?", (total_bytes, game_id)
    )

    seen: set[str] = set()
    paths: list[str] = []
    for chunk in manifest.chunks:
        p = epic_chunk_path(chunk, manifest.version)
        if p not in seen:
            seen.add(p)
            paths.append(p)

    settings = get_settings()
    result = await epic_prefill_chunks(paths, cdn_host, cdn_base, settings)
    if result.chunks_failed > 0:
        await deps.pool.execute_write(
            "UPDATE games SET status='failed', last_error=? WHERE id=?",
            (
                f"prefill: {result.chunks_failed}/{result.chunks_total} chunks failed"[:200],
                game_id,
            ),
        )
        raise RuntimeError(
            f"epic prefill failed: {result.chunks_failed}/{result.chunks_total} chunks"
        )

    # Inline header-HIT verification (epic validation; F7-epic disk-stat deferred).
    hit_ratio = await epic_verify_cached(paths[:20], cdn_host, cdn_base, settings)
    if paths and hit_ratio < 0.5:
        _log.warning(
            "prefill.epic.low_hit_ratio",
            job_id=job_id,
            game_id=game_id,
            hit_ratio=round(hit_ratio, 3),
        )
    await deps.pool.execute_write(
        "UPDATE games SET status='up_to_date', last_prefilled_at=CURRENT_TIMESTAMP WHERE id=?",
        (game_id,),
    )
    _log.info(
        "prefill.epic.completed",
        job_id=job_id,
        game_id=game_id,
        total=result.chunks_total,
        hit_ratio=round(hit_ratio, 3),
    )


async def _steam_prefill(job: dict[str, Any], deps: Deps) -> None:
    """Prefill one Steam game's chunks through the lancache (F5).

    Raises:
        ValueError — unknown game or non-steam platform.
        RuntimeError — steam_client is None, or chunks failed to download.
    """
    if deps.steam_client is None:
        raise RuntimeError("steam_client is required for prefill handler")
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("prefill job has no game_id")

    game = await deps.pool.read_one("SELECT id, app_id, platform FROM games WHERE id=?", (game_id,))
    if game is None:
        raise ValueError(f"game {game_id} not found in games table")
    if game["platform"] != "steam":
        raise ValueError(f"game {game_id} platform is {game['platform']!r}, not steam")

    job_id = job.get("id")
    await deps.pool.execute_write("UPDATE games SET status='downloading' WHERE id=?", (game_id,))
    _log.info("prefill.started", job_id=job_id, game_id=game_id)

    # Ensure manifests exist (fetch once if the game has none).
    uris = await _load_chunk_uris(deps, game_id)
    if not uris:
        existing = await deps.pool.read_one(
            "SELECT 1 AS one FROM manifests WHERE game_id=? AND depot_id IS NOT NULL LIMIT 1",
            (game_id,),
        )
        if existing is None:
            try:
                app_id_int = int(game["app_id"])
            except (TypeError, ValueError) as e:
                raise ValueError(f"game {game_id} app_id not numeric") from e
            _log.info("prefill.fetching_manifests", job_id=job_id, game_id=game_id)
            await deps.steam_client.manifest_fetch(app_id_int)
            uris = await _load_chunk_uris(deps, game_id)

    settings = get_settings()
    result = await prefill_chunks(uris, settings)
    _log.info(
        "prefill.completed",
        job_id=job_id,
        game_id=game_id,
        total=result.chunks_total,
        ok=result.chunks_ok,
        failed=result.chunks_failed,
    )

    if result.chunks_failed > 0:
        await deps.pool.execute_write(
            "UPDATE games SET status='failed', last_error=? WHERE id=?",
            (
                f"prefill: {result.chunks_failed}/{result.chunks_total} chunks failed"[:200],
                game_id,
            ),
        )
        raise RuntimeError(f"prefill failed: {result.chunks_failed}/{result.chunks_total} chunks")

    # ID5: success → enqueue a validate job (it sets the final status). The
    # jobs.source CHECK allows scheduler/cli/gameshelf/api — use 'scheduler'
    # for this automated enqueue.
    await deps.pool.execute_write(
        "INSERT INTO jobs (kind, game_id, platform, state, source) "
        "VALUES ('validate', ?, 'steam', 'queued', 'scheduler')",
        (game_id,),
    )
    await deps.pool.execute_write(
        "UPDATE games SET last_prefilled_at=CURRENT_TIMESTAMP WHERE id=?", (game_id,)
    )
    _log.info("prefill.validate_enqueued", job_id=job_id, game_id=game_id)
