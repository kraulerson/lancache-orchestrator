"""POST /api/v1/new-games-check — manually enqueue a new_games_check job.

Reuses the in-flight dedup: when a check is already queued/running its id is
returned with ``queued=false``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
from orchestrator.scheduler.jobs import enqueue_new_games_check

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["new-games"])


@router.post(
    "/new-games-check",
    responses={
        202: {"description": "Check queued (or existing in-flight check returned)"},
        401: {"description": "Missing/invalid bearer"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_new_games_check(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        inserted = await enqueue_new_games_check(pool, source="api")
        row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='new_games_check' "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1"
        )
        if row is None:
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("new_games_trigger.queued", job_id=int(row["id"]), queued=bool(inserted))
        return JSONResponse(
            status_code=202,
            content={"job_id": int(row["id"]), "queued": bool(inserted)},
        )
    except PoolError as e:
        _log.error("new_games_trigger.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
