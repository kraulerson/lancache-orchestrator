"""POST /api/v1/games/{game_id}/manifest/fetch — manifest fetch trigger (BL12).

Handler-side dedup: if a `manifest_fetch` job for this game is already
queued/running, return the existing job_id rather than creating a
duplicate. Race window between the SELECT and INSERT can yield two
queued rows on concurrent POSTs — accepted per the same trade-off as
the library_sync trigger endpoint (handler is idempotent on UPSERT).
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
router = APIRouter(prefix="/api/v1/games", tags=["manifest"])


@router.post(
    "/{game_id}/manifest/fetch",
    responses={
        202: {"description": "Manifest fetch job queued or existing in-flight job returned"},
        400: {"description": "Game is on a non-steam platform"},
        401: {"description": "Missing/invalid bearer"},
        404: {"description": "Game not found"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_manifest_fetch(
    game_id: int,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        game = await pool.read_one("SELECT id, platform FROM games WHERE id=?", (game_id,))
        if game is None:
            raise HTTPException(status_code=404, detail=f"game {game_id} not found")
        if game["platform"] != "steam":
            raise HTTPException(
                status_code=400,
                detail=f"manifest fetch only supports steam (got {game['platform']!r})",
            )

        existing = await pool.read_one(
            "SELECT id FROM jobs "
            "WHERE kind='manifest_fetch' AND game_id=? "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1",
            (game_id,),
        )
        if existing is not None:
            _log.info(
                "manifest_trigger.dedup_hit",
                game_id=game_id,
                existing_job_id=existing["id"],
            )
            return JSONResponse(status_code=202, content={"job_id": int(existing["id"])})

        # ON CONFLICT DO NOTHING + the migration-0007 partial UNIQUE index make
        # this race-safe (UAT-11 F-INT-5): a concurrent in-flight manifest_fetch
        # for this game makes our INSERT a no-op and we return the winner's job_id.
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "VALUES ('manifest_fetch', ?, 'steam', 'queued', 'api') ON CONFLICT DO NOTHING",
            (game_id,),
        )
        new_row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='manifest_fetch' AND game_id=? "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1",
            (game_id,),
        )
        if new_row is None:
            _log.error("manifest_trigger.insert_invisible_after_write", game_id=game_id)
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("manifest_trigger.queued", game_id=game_id, job_id=int(new_row["id"]))
        return JSONResponse(status_code=202, content={"job_id": int(new_row["id"])})
    except HTTPException:
        raise
    except PoolError as e:
        _log.error("manifest_trigger.db_unavailable", game_id=game_id, reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
