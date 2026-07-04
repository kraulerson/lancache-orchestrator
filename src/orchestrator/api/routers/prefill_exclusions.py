"""Prefill-exclusion overrides (#225) — the operator's control over the
auto-classify-block list.

`GET  /api/v1/prefill-exclusions`                — list all exclusions/allows.
`POST /api/v1/prefill-exclusions/{platform}/{app_id}`  — set mode (allow|exclude),
        source='operator'. A `mode='allow'` row is the STICKY override: the
        auto-classify step never overwrites it, so an un-excluded game keeps
        being cached and is never re-flagged.
`DELETE /api/v1/prefill-exclusions/{platform}/{app_id}` — clear the override.
`PUT  /api/v1/prefill-exclusions/gameshelf/{platform}` — Game_shelf reconciles the
        FULL set of `source='gameshelf'` cross-launcher exclusions for a platform
        (Piece 3, #446). Manages only its own rows; never clobbers operator/classifier.

Read/write only the `prefill_exclusions` table; never touches the cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)

_PLATFORMS = {"steam", "epic"}


class PrefillExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str
    app_id: str
    mode: str
    reason: str | None
    source: str
    updated_at: str


class PrefillExclusionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exclusions: list[PrefillExclusion]
    total: int


class SetExclusionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["allow", "exclude"]
    reason: str | None = None


# app_id length mirrors the table's CHECK (length(app_id) BETWEEN 1 AND 64), so a
# bad id is rejected as 400 at the edge rather than surfacing a 503 CHECK failure.
_AppId = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class GameshelfReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The FULL set of app_ids Game_shelf currently considers covered on a
    # higher-priority launcher. The endpoint makes the source='gameshelf' rows
    # match exactly. Bounded to keep a bad caller from unbounded work.
    app_ids: list[_AppId] = Field(default_factory=list, max_length=50000)


router = APIRouter(prefix="/api/v1", tags=["prefill-exclusions"])


@router.get(
    "/prefill-exclusions",
    response_model=PrefillExclusionsResponse,
    responses={200: {"description": "All prefill exclusions/allows"}, 503: {"description": "Pool"}},
    summary="List prefill exclusions",
)
async def list_prefill_exclusions(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        rows = await pool.read_all(
            "SELECT platform, app_id, mode, reason, source, updated_at "
            "FROM prefill_exclusions ORDER BY mode, platform, app_id"
        )
    except PoolError as e:
        _log.error("api.prefill_exclusions.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    items = [dict(r) for r in rows]
    body = PrefillExclusionsResponse(
        exclusions=[PrefillExclusion(**r) for r in items], total=len(items)
    )
    return JSONResponse(content=body.model_dump(by_alias=True))


@router.post(
    "/prefill-exclusions/{platform}/{app_id}",
    responses={
        200: {"description": "Override set"},
        400: {"description": "Unknown platform"},
        503: {"description": "Pool"},
    },
    summary="Set a prefill-exclusion override (allow|exclude, operator source)",
)
async def set_prefill_exclusion(
    platform: str,
    app_id: str,
    body: SetExclusionRequest,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    if platform not in _PLATFORMS:
        return JSONResponse(content={"detail": "platform must be steam or epic"}, status_code=400)
    try:
        await pool.execute_write(
            "INSERT INTO prefill_exclusions (platform, app_id, mode, reason, source, updated_at) "
            "VALUES (?, ?, ?, ?, 'operator', CURRENT_TIMESTAMP) "
            "ON CONFLICT(platform, app_id) DO UPDATE SET "
            "  mode=excluded.mode, reason=excluded.reason, source='operator', "
            "  updated_at=CURRENT_TIMESTAMP",
            (platform, app_id, body.mode, body.reason),
        )
    except PoolError as e:
        _log.error("api.prefill_exclusions.write_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    return JSONResponse(content={"platform": platform, "app_id": app_id, "mode": body.mode})


@router.put(
    "/prefill-exclusions/gameshelf/{platform}",
    responses={
        200: {"description": "Gameshelf-sourced exclusions reconciled"},
        400: {"description": "Unknown platform or invalid app_id"},
        503: {"description": "Pool"},
    },
    summary="Reconcile the Game_shelf cross-launcher exclusion set for a platform",
)
async def reconcile_gameshelf_exclusions(
    platform: str,
    body: GameshelfReconcileRequest,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    """Make the source='gameshelf' exclude rows for ``platform`` match ``app_ids``
    exactly (Piece 3, #446). Game_shelf pushes the full current set of app_ids it
    considers covered on a higher-priority launcher (e.g. an Epic copy also owned
    on Steam); this endpoint inserts any missing ones and deletes any stale ones.

    Insert uses ON CONFLICT DO NOTHING so it NEVER overwrites an operator 'allow'
    (the sticky override) or an existing classifier/operator row — a game already
    covered by another source is left as-is. Delete is scoped to
    ``source='gameshelf'`` (and this platform), so operator and classifier rows are
    never touched. Insert + delete run in one transaction so the set is never left
    half-reconciled. Idempotent.
    """
    if platform not in _PLATFORMS:
        return JSONResponse(content={"detail": "platform must be steam or epic"}, status_code=400)
    app_ids = sorted(set(body.app_ids))
    try:
        async with pool.write_transaction() as tx:
            added = 0
            if app_ids:
                added = await tx.execute_many(
                    "INSERT INTO prefill_exclusions (platform, app_id, mode, reason, source) "
                    "VALUES (?, ?, 'exclude', 'gameshelf: covered on higher-priority launcher', "
                    "        'gameshelf') "
                    "ON CONFLICT(platform, app_id) DO NOTHING",
                    [(platform, app_id) for app_id in app_ids],
                )
                # Delete stale gameshelf rows for this platform not in the pushed
                # set. Placeholders are '?' only — the app_id values flow as bound
                # params (no interpolation of caller data into the SQL text).
                placeholders = ", ".join("?" for _ in app_ids)
                delete_sql = (
                    "DELETE FROM prefill_exclusions "  # noqa: S608
                    "WHERE platform = ? AND source = 'gameshelf' "
                    f"AND app_id NOT IN ({placeholders})"
                )
                removed = await tx.execute(delete_sql, (platform, *app_ids))
            else:
                # Empty push clears every gameshelf row for the platform.
                removed = await tx.execute(
                    "DELETE FROM prefill_exclusions WHERE platform = ? AND source = 'gameshelf'",
                    (platform,),
                )
    except PoolError as e:
        _log.error("api.prefill_exclusions.reconcile_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    _log.info(
        "api.prefill_exclusions.gameshelf_reconciled",
        platform=platform,
        total=len(app_ids),
        added=added,
        removed=removed,
    )
    return JSONResponse(
        content={"platform": platform, "added": added, "removed": removed, "total": len(app_ids)}
    )


@router.delete(
    "/prefill-exclusions/{platform}/{app_id}",
    responses={200: {"description": "Override cleared (idempotent)"}, 503: {"description": "Pool"}},
    summary="Clear a prefill-exclusion override",
)
async def clear_prefill_exclusion(
    platform: str,
    app_id: str,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        deleted = await pool.execute_write(
            "DELETE FROM prefill_exclusions WHERE platform = ? AND app_id = ?",
            (platform, app_id),
        )
    except PoolError as e:
        _log.error("api.prefill_exclusions.delete_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    return JSONResponse(content={"deleted": deleted})
