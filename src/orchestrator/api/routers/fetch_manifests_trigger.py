"""POST /api/v1/fetch-manifests — enqueue a DepotDownloader manifest-only fetch.

Closes the validation-coverage gap: the agent self-enumerates the cached app set
and fetches manifests (no chunk bytes) so the validator can cover apps that
SteamPrefill skipped. Reuses the fetch_manifests in-flight dedup
(idx_jobs_fetch_manifests_inflight, migration 0009).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
from orchestrator.scheduler.jobs import enqueue_fetch_manifests

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["fetch_manifests"])


@router.post(
    "/fetch-manifests",
    responses={
        202: {"description": "Fetch queued (or existing in-flight fetch returned)"},
        401: {"description": "Missing/invalid bearer"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_fetch_manifests(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        inserted = await enqueue_fetch_manifests(pool, source="api")
        row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='fetch_manifests' "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1"
        )
        if row is None:
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        body: dict[str, Any] = {"job_id": int(row["id"]), "queued": bool(inserted)}
        _log.info(
            "fetch_manifests_trigger.queued",
            job_id=body["job_id"],
            queued=body["queued"],
        )
        return JSONResponse(status_code=202, content=body)
    except PoolError as e:
        _log.error("fetch_manifests_trigger.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
