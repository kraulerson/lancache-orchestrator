"""POST /api/v1/sweep — manually enqueue a validation sweep (F13).

`{"full": true}` runs the validate-all backfill over EVERY steam game (used after
seeding the durable manifest archive); the default re-validates only the cached
library, same as the weekly cron. Reuses the sweep in-flight dedup."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
from orchestrator.scheduler.jobs import enqueue_validation_sweep

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["sweep"])


class SweepTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full: bool = False


@router.post(
    "/sweep",
    responses={
        202: {"description": "Sweep queued (or existing in-flight sweep returned)"},
        401: {"description": "Missing/invalid bearer"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_sweep(
    body: SweepTriggerRequest | None = None,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    full = bool(body.full) if body is not None else False
    try:
        inserted = await enqueue_validation_sweep(pool, full=full, source="api")
        row = await pool.read_one(
            "SELECT id, payload FROM jobs WHERE kind='sweep' "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1"
        )
        if row is None:
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        # Report the ACTUAL in-flight job's mode, not what THIS call requested.
        # When a non-full sweep is already queued, the ON CONFLICT DO NOTHING
        # insert is a no-op and the operator's full=true request did NOT take
        # effect — surface that via the real payload + a `queued` flag so the
        # CLI can warn (parse robustly; a malformed stored payload must not 500).
        actual_full: bool = False
        try:
            actual_full = bool(json.loads(row["payload"] or "{}").get("full", False))
        except (json.JSONDecodeError, TypeError, AttributeError):
            actual_full = False
        _log.info(
            "sweep_trigger.queued",
            job_id=int(row["id"]),
            requested_full=full,
            actual_full=actual_full,
            queued=bool(inserted),
        )
        return JSONResponse(
            status_code=202,
            content={"job_id": int(row["id"]), "full": actual_full, "queued": bool(inserted)},
        )
    except PoolError as e:
        _log.error("sweep_trigger.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
