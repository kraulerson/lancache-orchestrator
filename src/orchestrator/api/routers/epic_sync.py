"""POST /api/v1/platforms/epic/library/sync — manual Epic library-sync trigger (F6).

Parallel to the Steam sync route. Dedup is DB-enforced by the partial UNIQUE index
``idx_jobs_library_sync_inflight`` (per-platform), so a concurrent cron + API race
can't yield two queued rows. Returns the id of the single in-flight job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/platforms/epic/library", tags=["epic-sync"])


@router.post(
    "/sync",
    responses={
        202: {"description": "Job queued or existing in-flight job returned"},
        401: {"description": "Missing/invalid bearer"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_epic_library_sync(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES (?, ?, 'queued', 'api') ON CONFLICT DO NOTHING",
            ("library_sync", "epic"),
        )
        row = await pool.read_one(
            "SELECT id FROM jobs "
            "WHERE kind='library_sync' AND platform='epic' "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1"
        )
        if row is None:
            _log.error("epic_sync.library.inflight_row_not_found", inserted=int(inserted))
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        job_id = int(row["id"])
        if inserted:
            _log.info("epic_sync.library.queued", job_id=job_id)
        else:
            _log.info("epic_sync.library.dedup_hit", existing_job_id=job_id)
        return JSONResponse(status_code=202, content={"job_id": job_id})
    except PoolError as e:
        _log.error("epic_sync.library.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
