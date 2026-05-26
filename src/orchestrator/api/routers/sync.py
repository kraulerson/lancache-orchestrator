"""POST /api/v1/platforms/steam/library/sync — manual library-sync trigger (BL11).

Handler-side dedup: if a `library_sync` job for `steam` is already
queued or running, return its existing job_id rather than creating a
duplicate. Race window between the SELECT and INSERT can yield two
queued rows on concurrent POSTs — accepted per plan P8 since the
worker loop's second handler call is idempotent (UPSERT semantics).
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
        existing = await pool.read_one(
            "SELECT id FROM jobs "
            "WHERE kind='library_sync' AND platform='steam' "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1"
        )
        if existing is not None:
            _log.info("sync.library.dedup_hit", existing_job_id=existing["id"])
            return JSONResponse(status_code=202, content={"job_id": int(existing["id"])})

        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
            ("library_sync", "steam"),
        )
        new_row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='library_sync' AND platform='steam' "
            "AND state='queued' ORDER BY id DESC LIMIT 1"
        )
        if new_row is None:
            # execute_write succeeded but the row isn't visible — pool/replica anomaly.
            _log.error("sync.library.insert_invisible_after_write")
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("sync.library.queued", job_id=int(new_row["id"]))
        return JSONResponse(status_code=202, content={"job_id": int(new_row["id"])})
    except PoolError as e:
        _log.error("sync.library.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
