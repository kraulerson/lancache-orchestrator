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
            "SELECT id FROM jobs "
            "WHERE kind='prefill' AND game_id=? "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1",
            (game_id,),
        )
        if existing is not None:
            _log.info(
                "prefill_trigger.dedup_hit",
                game_id=game_id,
                existing_job_id=existing["id"],
            )
            return JSONResponse(status_code=202, content={"job_id": int(existing["id"])})

        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "VALUES ('prefill', ?, ?, 'queued', 'api')",
            (game_id, platform),
        )
        new_row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='prefill' AND game_id=? "
            "AND state='queued' ORDER BY id DESC LIMIT 1",
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
