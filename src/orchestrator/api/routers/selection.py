"""GET /api/v1/selection/candidates — Steam prefill-selection review (#229).

Lists apps in the store-info cache that look like non-games (soundtracks, tools,
SDKs, dedicated servers, demos, videos) — CANDIDATES for the operator to remove
from ``selectedAppsToPrefill.json``. Read-only; it never edits the selection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
from orchestrator.platform.steam.selection_classifier import classify

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)


class SelectionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: str
    name: str
    app_type: str
    reason: str


class SelectionCandidatesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[SelectionCandidate]
    total_candidates: int
    total_scanned: int


router = APIRouter(prefix="/api/v1", tags=["selection"])


@router.get(
    "/selection/candidates",
    response_model=SelectionCandidatesResponse,
    responses={
        200: {"description": "Prefill-exclusion candidates"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List prefill-selection exclusion candidates",
    description=(
        "Classifies every Steam app in the store-info cache (`steam_app_info`, "
        "populated by library_sync) and returns those that look like non-games — "
        "soundtracks, tools/SDKs, dedicated servers, demos, videos. These are "
        "CANDIDATES to remove from `selectedAppsToPrefill.json`; the endpoint "
        "changes nothing. Apps Steam types as `game` that are really utilities "
        "(e.g. Lossless Scaling) stay an operator judgement call."
    ),
)
async def selection_candidates(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    try:
        rows = await pool.read_all(
            "SELECT app_id, app_type, name FROM steam_app_info ORDER BY app_type, name"
        )
    except PoolError as e:
        _log.error("api.selection.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)

    candidates: list[SelectionCandidate] = []
    for row in rows:
        reason = classify(row["app_type"], row["name"])
        if reason is not None:
            candidates.append(
                SelectionCandidate(
                    app_id=row["app_id"],
                    name=row["name"],
                    app_type=row["app_type"],
                    reason=reason,
                )
            )

    body = SelectionCandidatesResponse(
        candidates=candidates,
        total_candidates=len(candidates),
        total_scanned=len(rows),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))
