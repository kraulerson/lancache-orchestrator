"""POST /api/v1/games/{game_id}/prefill — prefill trigger (F5 steam, F6 epic).

Supports both steam and epic games; the enqueued job carries the game's own
platform so the worker dispatches to the right prefill handler.

Handler-side dedup: if a `prefill` job for this game is already queued/running,
return the existing job_id rather than creating a duplicate. The race window
between SELECT and INSERT can yield two queued rows on concurrent POSTs —
accepted, as the prefill handler is idempotent (re-requesting cached chunks
just HITs the cache).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/games", tags=["prefill"])

# Job-payload marker for a FORCED prefill — threads SteamPrefill `--force`, which
# re-requests every chunk so lancache refills evicted/partial games (a normal
# prefill skips an app SteamPrefill's own state thinks is already complete).
# Single source of truth for the INSERT, the dedup force-upgrade, and the
# handler's payload parse (jobs/handlers/prefill.py:_payload_force).
_FORCE_PAYLOAD = '{"force": true}'


@router.post(
    "/{game_id}/prefill",
    responses={
        202: {"description": "Prefill job queued or existing in-flight job returned"},
        400: {"description": "Game is on an unsupported platform (not steam/epic)"},
        401: {"description": "Missing/invalid bearer"},
        404: {"description": "Game not found"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_prefill(
    game_id: int,
    force: bool = False,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        game = await pool.read_one("SELECT id, platform FROM games WHERE id=?", (game_id,))
        if game is None:
            raise HTTPException(status_code=404, detail=f"game {game_id} not found")
        platform = game["platform"]
        if platform not in ("steam", "epic"):
            raise HTTPException(
                status_code=400,
                detail=f"prefill only supports steam/epic (got {platform!r})",
            )

        existing = await pool.read_one(
            "SELECT id, state, payload FROM jobs "
            "WHERE kind='prefill' AND game_id=? "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1",
            (game_id,),
        )
        if existing is not None:
            # Force-upgrade: a force request that dedups onto a still-QUEUED
            # non-force prefill rewrites its payload so the force isn't silently
            # swallowed by the in-flight dedup (migration-0006 allows only one
            # prefill per game). A RUNNING prefill can't change mid-flight, so
            # it's returned as-is.
            if force and existing["state"] == "queued" and existing["payload"] != _FORCE_PAYLOAD:
                await pool.execute_write(
                    "UPDATE jobs SET payload=? WHERE id=?",
                    (_FORCE_PAYLOAD, existing["id"]),
                )
                _log.info(
                    "prefill_trigger.force_upgraded",
                    game_id=game_id,
                    existing_job_id=int(existing["id"]),
                )
            else:
                _log.info(
                    "prefill_trigger.dedup_hit",
                    game_id=game_id,
                    existing_job_id=existing["id"],
                )
            return JSONResponse(status_code=202, content={"job_id": int(existing["id"])})

        # ON CONFLICT DO NOTHING + the migration-0006 partial UNIQUE index make
        # this race-safe (audit 2026-06-09): if a concurrent POST already queued
        # an in-flight prefill for this game, our INSERT is a no-op and we return
        # the winner's job_id below — no duplicate prefill row, no duplicate work.
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source, payload) "
            "VALUES ('prefill', ?, ?, 'queued', 'api', ?) ON CONFLICT DO NOTHING",
            (game_id, platform, _FORCE_PAYLOAD if force else None),
        )
        new_row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='prefill' AND game_id=? "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1",
            (game_id,),
        )
        if new_row is None:
            _log.error("prefill_trigger.insert_invisible_after_write", game_id=game_id)
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("prefill_trigger.queued", game_id=game_id, job_id=int(new_row["id"]))
        return JSONResponse(status_code=202, content={"job_id": int(new_row["id"])})
    except HTTPException:
        raise
    except PoolError as e:
        _log.error("prefill_trigger.db_unavailable", game_id=game_id, reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
