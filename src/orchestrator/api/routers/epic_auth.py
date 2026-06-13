"""Epic OAuth submission + status (F6).

POST /api/v1/platforms/epic/auth — exchange a legendary.gl/epiclogin authorization
code for tokens, persist the refresh token, mark the platforms row authenticated,
and auto-enqueue an Epic library_sync. GET returns the epic auth status. The auth
code and tokens are NEVER echoed in responses or logs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.core.settings import get_settings
from orchestrator.db.pool import PoolError
from orchestrator.platform.epic.oauth import (
    EpicAuthError,
    exchange_code,
    save_refresh_token,
)

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/platforms/epic", tags=["epic-auth"])


class AuthCodeBody(BaseModel):
    code: str


@router.post(
    "/auth",
    responses={
        202: {"description": "Authenticated; library_sync enqueued"},
        401: {"description": "Missing/invalid bearer, or Epic rejected the code"},
        503: {"description": "Database unavailable"},
    },
)
async def submit_epic_auth(
    body: AuthCodeBody,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    settings = get_settings()
    try:
        tokens = await exchange_code(body.code, settings)
    except EpicAuthError:
        _log.warning("epic_auth.exchange_rejected")
        return JSONResponse(status_code=401, content={"detail": "epic authentication failed"})

    try:
        save_refresh_token(str(settings.epic_session_path), tokens.refresh_token)
    except OSError as e:
        # The OAuth code is now consumed, but we couldn't persist the refresh
        # token (read-only/full FS, symlink at the path, perms). Surface a clean
        # 503 instead of leaking an unhandled OSError as a 500 — and never
        # reflect the tokens (audit 2026-06-09).
        _log.error("epic_auth.persist_failed", error_type=type(e).__name__)
        return JSONResponse(
            status_code=503, content={"detail": "could not persist Epic credentials"}
        )
    try:
        await pool.execute_write(
            "UPDATE platforms SET auth_status='ok', auth_expires_at=?, last_error=NULL "
            "WHERE name='epic'",
            (tokens.expires_at or None,),
        )
        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('library_sync', 'epic', 'queued', 'api') ON CONFLICT DO NOTHING"
        )
    except PoolError as e:
        _log.error("epic_auth.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    _log.info("epic_auth.authenticated", account_id=tokens.account_id)
    return JSONResponse(
        status_code=202,
        content={"account_id": tokens.account_id, "display_name": tokens.display_name},
    )


@router.get(
    "/auth",
    responses={
        200: {"description": "Epic auth status"},
        401: {"description": "Missing/invalid bearer"},
        503: {"description": "Database unavailable"},
    },
)
async def epic_auth_status(
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        row = await pool.read_one(
            "SELECT auth_status, auth_expires_at, last_error FROM platforms WHERE name='epic'"
        )
    except PoolError as e:
        _log.error("epic_auth.status.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    if row is None:
        return JSONResponse(status_code=503, content={"detail": "epic platform row missing"})
    return JSONResponse(
        status_code=200,
        content={
            "auth_status": row["auth_status"],
            "auth_expires_at": row["auth_expires_at"],
            "last_error": row["last_error"],
        },
    )
