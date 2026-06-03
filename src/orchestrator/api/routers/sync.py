"""POST /api/v1/platforms/steam/library/sync — manual library-sync trigger (BL11).

Dedup is DB-enforced (migration 0004): the partial UNIQUE index
`idx_jobs_library_sync_inflight` permits at most one queued/running
`library_sync` per platform. The insert uses `ON CONFLICT DO NOTHING`, so a
concurrent cron + API race can no longer yield two queued rows (the previous
app-level SELECT-then-INSERT straddled an await — code review 2026-06-02).
Whether we inserted or deduped, we return the id of the single in-flight job.
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
router = APIRouter(prefix="/api/v1/platforms/steam/library", tags=["sync"])


@router.post(
    "/sync",
    responses={
        202: {"description": "Job queued or existing in-flight job returned"},
        401: {"description": "Missing/invalid bearer"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_library_sync(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES (?, ?, 'queued', 'api') ON CONFLICT DO NOTHING",
            ("library_sync", "steam"),
        )
        # The partial UNIQUE index guarantees exactly one in-flight library_sync
        # per platform — fetch it whether we just inserted it or deduped onto an
        # existing one.
        row = await pool.read_one(
            "SELECT id FROM jobs "
            "WHERE kind='library_sync' AND platform='steam' "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1"
        )
        if row is None:
            # Either the write wasn't visible, or the in-flight job we deduped
            # onto completed in the gap between insert and select — a pool/timing
            # anomaly the caller can simply retry.
            _log.error("sync.library.inflight_row_not_found", inserted=int(inserted))
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        job_id = int(row["id"])
        if inserted:
            _log.info("sync.library.queued", job_id=job_id)
        else:
            _log.info("sync.library.dedup_hit", existing_job_id=job_id)
        return JSONResponse(status_code=202, content={"job_id": job_id})
    except PoolError as e:
        _log.error("sync.library.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
