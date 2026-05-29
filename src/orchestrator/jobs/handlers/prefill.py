"""F5 — prefill job handler.

Builds the game's deduped chunk list (latest manifest per depot →
``manifest.expand``, reusing F7's query) and downloads each chunk through the
lancache so it gets cached. On full success, enqueues a ``validate`` job (ID5),
which sets the game's final status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.prefill.downloader import prefill_chunks, steam_chunk_download_uri
from orchestrator.validator.disk_stat import _LATEST_PER_DEPOT_SQL

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


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
    """Prefill one Steam game's chunks through the lancache (F5).

    Raises:
        ValueError — non-steam platform or unknown game.
        RuntimeError — steam_client is None, or chunks failed to download.
    """
    if job.get("platform") != "steam":
        raise ValueError(f"prefill only supports steam (got {job.get('platform')!r})")
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
