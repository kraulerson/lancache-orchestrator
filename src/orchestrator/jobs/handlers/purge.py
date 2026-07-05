"""F18 — purge job handler.

Deletes a game's cached chunk files via the data-plane agent (which alone holds
the cache filesystem), then sets ``games.status='validation_failed'`` so the
existing F5/F6 re-prefill path re-downloads a clean copy (ADR-0015 — purge is
reversible). Steam enumerates chunks agent-side from its own manifest cache; Epic
sends the stored manifest (version, cdn_base, raw bytes) exactly as validate does.

An ``AgentError`` from the delete propagates (the worker marks the job failed) and
leaves ``games.status`` untouched — a failed delete must not falsely flag the game.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from orchestrator.clients.agent_client import AgentClient
    from orchestrator.db.pool import Pool
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


async def _purge_epic_game(
    agent: AgentClient, pool: Pool, game_id: int, app_id: str
) -> dict[str, Any]:
    """Load the game's stored Epic manifest (version, cdn_base, raw) — exactly as
    the validate handler's Epic branch does — and delegate the delete to the agent.
    A game with no fetched manifest cannot be enumerated, so this raises a clear
    error rather than silently no-op'ing (ADR-0015)."""
    manifest = await pool.read_one(
        "SELECT version, cdn_base, raw FROM manifests "
        "WHERE game_id=? ORDER BY fetched_at DESC LIMIT 1",
        (game_id,),
    )
    if manifest is None:
        raise ValueError(f"epic game {game_id} has no manifest to purge")
    if not manifest["cdn_base"]:
        raise ValueError(f"epic game {game_id} manifest has no cdn_base (re-prefill first)")
    try:
        app_id_int = int(app_id)
    except (TypeError, ValueError):
        app_id_int = 0
    return await agent.epic_purge(
        app_id=app_id_int,
        version=str(manifest["version"]),
        cdn_base=str(manifest["cdn_base"]),
        raw_manifest_b64=base64.b64encode(manifest["raw"]).decode("ascii"),
    )


async def purge_handler(job: dict[str, Any], deps: Deps) -> None:
    """Purge one game's cached chunks (F18), then flag it for re-prefill.

    Raises:
        ValueError — unsupported platform, missing/unknown game, agent unavailable,
            non-numeric Steam app_id, or (Epic) no manifest to enumerate.
        AgentError — the agent-side delete failed; propagates so the job is marked
            failed and the game's status is left unchanged.
    """
    platform = job.get("platform")
    if platform not in ("steam", "epic"):
        raise ValueError(f"purge supports steam+epic (got {platform!r})")
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("purge job has no game_id")
    agent = deps.agent_client
    if agent is None:
        raise ValueError("purge requires the data-plane agent (agent_client unavailable)")

    game = await deps.pool.read_one("SELECT id, app_id FROM games WHERE id=?", (game_id,))
    if game is None:
        raise ValueError(f"game {game_id} not found in games table")

    job_id = job.get("id")
    _log.info("purge.started", job_id=job_id, game_id=game_id, platform=platform)

    if platform == "steam":
        try:
            app_id_int = int(game["app_id"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"steam app_id not numeric: {game['app_id']!r}") from e
        result = await agent.steam_purge(app_id_int)
    else:  # epic
        result = await _purge_epic_game(agent, deps.pool, game_id, game["app_id"])

    files_deleted = int(result.get("deleted", 0))
    files_failed = int(result.get("failed", 0))
    bytes_freed = int(result.get("bytes_freed", 0))

    # Reversibility invariant: purge sets validation_failed so F5/F6 re-prefills a
    # fresh copy. Conditional to avoid churn when the game was already flagged (a
    # {deleted:0} idempotent re-purge still lands here harmlessly).
    await deps.pool.execute_write(
        "UPDATE games SET status='validation_failed' WHERE id=? AND status != 'validation_failed'",
        (game_id,),
    )
    _log.info(
        "game.purged",
        job_id=job_id,
        game_id=game_id,
        platform=platform,
        app_id=game["app_id"],
        files_deleted=files_deleted,
        files_failed=files_failed,
        total_bytes_freed=bytes_freed,
    )
