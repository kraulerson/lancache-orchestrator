"""POST /api/v1/games/{game_id}/purge — operator-driven cache purge trigger (F18).

Enqueues a `purge` job carrying the game's own platform so the worker dispatches
to the right agent purge endpoint. Keyed by game_id for consistency with the
sibling prefill/validate triggers (and so Game_shelf's proxy is a trivial copy).

Handler-side dedup: if a `purge` job for this game is already queued/running,
return the existing job_id rather than creating a duplicate. Backed by migration
0014's partial UNIQUE index + `INSERT ... ON CONFLICT DO NOTHING`, so concurrent
POSTs collapse onto one in-flight purge. The delete itself is idempotent (a
never-cached game returns {deleted:0}).
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
router = APIRouter(prefix="/api/v1/games", tags=["purge"])


@router.post(
    "/{game_id}/purge",
    responses={
        202: {"description": "Purge job queued or existing in-flight job returned"},
        400: {"description": "Game is on an unsupported platform (not steam/epic)"},
        401: {"description": "Missing/invalid bearer"},
        404: {"description": "Game not found"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_purge(
    game_id: int,
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
                detail=f"purge only supports steam/epic (got {platform!r})",
            )

        existing = await pool.read_one(
            "SELECT id FROM jobs "
            "WHERE kind='purge' AND game_id=? "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1",
            (game_id,),
        )
        if existing is not None:
            _log.info(
                "purge_trigger.dedup_hit",
                game_id=game_id,
                existing_job_id=existing["id"],
            )
            return JSONResponse(status_code=202, content={"job_id": int(existing["id"])})

        # ON CONFLICT DO NOTHING + migration-0014's idx_jobs_purge_inflight partial
        # UNIQUE index make this race-safe: a concurrent in-flight purge for this
        # game makes our INSERT a no-op and we return the winner's job_id.
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "VALUES ('purge', ?, ?, 'queued', 'api') ON CONFLICT DO NOTHING",
            (game_id, platform),
        )
        new_row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='purge' AND game_id=? "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1",
            (game_id,),
        )
        if new_row is None:
            _log.error("purge_trigger.insert_invisible_after_write", game_id=game_id)
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("purge_trigger.queued", game_id=game_id, job_id=int(new_row["id"]))
        return JSONResponse(status_code=202, content={"job_id": int(new_row["id"])})
    except HTTPException:
        raise
    except PoolError as e:
        _log.error("purge_trigger.db_unavailable", game_id=game_id, reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
