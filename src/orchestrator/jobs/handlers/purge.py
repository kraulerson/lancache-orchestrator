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

from orchestrator.core.settings import get_settings
from orchestrator.jobs.handlers.validate import validate_one_game
from orchestrator.jobs.measurement import record_measurement

if TYPE_CHECKING:
    from orchestrator.clients.agent_client import AgentClient
    from orchestrator.db.pool import Pool
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

# Same shape validate.py writes. method stays 'disk_stat' because that is the
# observation being recorded — the state of the chunk files on disk.
_INSERT_VH = (
    "INSERT INTO validation_history "
    "(game_id, manifest_version, started_at, finished_at, method, "
    " chunks_total, chunks_cached, chunks_missing, outcome, error) "
    "VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'disk_stat', ?, 0, ?, 'missing', NULL)"
)


async def _record_cache_emptied(pool: Pool, game_id: int, tx: Any = None) -> None:
    """Append an observation that nothing is cached any more (#293).

    ``chunks_cached`` is not stored on ``games`` — the API reads it from the newest
    ``validation_history`` row. Purge used to delete the files and record nothing, so
    the newest observation stayed the pre-purge one and every consumer kept reporting
    a fully cached game with no files behind it. Game_shelf's cache badge is driven by
    exactly those fields, so a purged game displayed "Cached 337/337".

    History is append-only: the previous row was true when it was written, so this
    adds a new observation rather than editing the old one.

    ``chunks_total`` is carried from the last validation because purge does not
    re-read the manifest. With no prior validation there is no known total, and
    inventing one would be its own false report — so nothing is written and the
    re-prefill flag alone carries the state.
    """
    reader = tx if tx is not None else pool
    previous = await reader.read_one(
        # chunks_total > 0 (#308, which #310 made reachable here): the failed
        # validation this falls back from appends its own 0-chunk error row, so
        # "the newest row" would take the size from a run that measured nothing
        # and report the game as 0 chunks. An error carries no size information.
        "SELECT manifest_version, chunks_total FROM validation_history "
        "WHERE game_id = ? AND chunks_total > 0 ORDER BY id DESC LIMIT 1",
        (game_id,),
    )
    if previous is None:
        return

    total = int(previous["chunks_total"])
    params = (game_id, previous["manifest_version"], total, total)
    if tx is not None:
        await tx.execute(_INSERT_VH, params)
    else:
        await pool.execute_write(_INSERT_VH, params)


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

    # Delete, THEN measure (#310). The agent returns HTTP 200 with
    # {"deleted": 0, "failed": N} when every unlink fails — which is what happens
    # when it comes back as uid 1000 instead of 0:0, as it has twice in this
    # project. This used to record 'partial' and a chunks_cached=0 observation
    # unconditionally, so an EACCES on every file wrote validation_failed over a
    # cache that was completely intact and queued the whole set for re-download.
    # files_failed was read and never acted on.
    #
    # So cache truth comes from a real validation of the disk afterwards, not from
    # the agent's own report of what it thinks it did. That also decides the
    # partial case honestly: 300 of 337 deleted records the 37 that survived,
    # where both "any failure is total failure" and "infer from the counts" get it
    # wrong in one direction or the other.
    #
    # Reversibility (ADR-0015) is unchanged: a measured-empty cache is
    # 'not_downloaded' and a measured-partial one 'validation_failed', both in the
    # set F5/F6 select on, and the single writer stamps status_measured_at, which
    # Epic's scheduled prefill also requires.
    #
    # commanded=True because the files are ALREADY gone by the time this runs
    # (security audit SEV-2). The breaker vetoes observations it cannot trust;
    # refusing this one would not preserve truth, it would discard the only record
    # of a delete that really happened — leaving a green badge over an empty
    # cache, ineligible for re-prefill, and unreachable by the sweep the same
    # breaker has halted. Since #310 that exemption is from the veto ONLY: the
    # transition row is written and marked, so a bulk purge is visible in the log
    # without arming the alarm against the next sweep.
    #
    # validate_one_game writes the history row and the measurement in ONE
    # transaction, which is the atomicity #293 needs: the status and the
    # observation behind it can never disagree.
    settings = get_settings()
    measured = await validate_one_game(deps.pool, deps, game_id, settings, commanded=True)

    if measured.outcome == "error" and files_deleted > 0:
        # The deletes happened; only the measurement failed. Leaving the pre-purge
        # status would put a green badge over a cache this system just emptied —
        # the exact defect 'commanded' was introduced to fix. Fall back to the
        # conservative record: something is missing, and nothing is claimed about
        # how much. With no successful delete there is nothing to correct, so the
        # attempt-only write validate_one_game already made is left to stand.
        async with deps.pool.write_transaction() as tx:
            await record_measurement(deps.pool, game_id, "partial", tx=tx, commanded=True)
            await _record_cache_emptied(deps.pool, game_id, tx)

    _log.info(
        "game.purged",
        job_id=job_id,
        game_id=game_id,
        platform=platform,
        app_id=game["app_id"],
        files_deleted=files_deleted,
        files_failed=files_failed,
        total_bytes_freed=bytes_freed,
        measured_outcome=measured.outcome,
        chunks_cached_after=measured.chunks_cached,
    )
