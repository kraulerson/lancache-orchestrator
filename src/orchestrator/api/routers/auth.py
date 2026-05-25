"""POST /api/v1/platforms/steam/auth* — Steam authentication (BL10 / F1)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Literal, TypedDict

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
from orchestrator.platform.steam.client import (
    IPCTimeoutError,
    SteamWorkerClient,
    SteamWorkerError,
    WorkerDiedError,
    WorkerDisabledError,
)

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

CHALLENGE_TTL_SEC = 300  # 5-min TTL per F1 D11
_log = structlog.get_logger(__name__)


class _ChallengeState(TypedDict):
    """In-memory state for an in-flight 2FA challenge.

    Issue #94: stores `username` alongside the expiry so `auth_complete`
    can write the correct platforms.config without reading back from the
    DB (which fails silently to empty-string on first-ever auth).
    """

    expires_at_mono: float
    username: str


# In-memory challenge state: challenge_id -> _ChallengeState
# Per F1 D11: 5-min TTL; server restart invalidates (acceptable).
_challenges: dict[str, _ChallengeState] = {}


def _sweep_expired_challenges() -> None:
    """Issue #95 item 3: evict expired challenges. Called from auth_begin
    so abandoned 2FA flows don't leak memory indefinitely."""
    now = time.monotonic()
    expired = [cid for cid, st in _challenges.items() if now > st["expires_at_mono"]]
    for cid in expired:
        _challenges.pop(cid, None)


# ---------- request/response models ----------


class AuthBeginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=512)


class AuthCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=64)


class AuthSuccessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["authenticated"] = "authenticated"
    steam_id: int


class AuthChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str
    challenge_type: Literal["mobile_authenticator", "email_code"]
    expires_at: str  # ISO8601


class AuthStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authenticated: bool
    steam_id: int | None = None
    last_check_at: str


# ---------- dependency injection seam ----------


_steam_client_singleton: SteamWorkerClient | None = None


def get_steam_client_dep() -> SteamWorkerClient:
    """FastAPI dependency for the shared SteamWorkerClient.

    Production: returns the singleton spawned in lifespan startup.
    Tests: override via app.dependency_overrides[get_steam_client_dep].
    """
    if _steam_client_singleton is None:
        raise HTTPException(status_code=503, detail="steam worker not initialized")
    return _steam_client_singleton


def set_steam_client_singleton(client: SteamWorkerClient | None) -> None:
    """Called from FastAPI lifespan startup (main.py) to publish the
    spawned worker into the DI singleton slot. Pass None at shutdown."""
    global _steam_client_singleton
    _steam_client_singleton = client


# ---------- router ----------


router = APIRouter(prefix="/api/v1/platforms/steam", tags=["auth"])


async def _update_platform_row_success(pool: Pool, *, steam_id: int, username: str) -> None:
    config_json = json.dumps(
        {
            "steam_id": steam_id,
            "username": username,
            "last_refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    await pool.execute_write(
        "UPDATE platforms SET auth_status='ok', last_sync_at=CURRENT_TIMESTAMP, "
        "last_error=NULL, config=? WHERE name='steam'",
        (config_json,),
    )


async def _update_platform_row_failure(pool: Pool, *, error: str) -> None:
    await pool.execute_write(
        "UPDATE platforms SET auth_status='error', last_error=? WHERE name='steam'",
        (error[:200],),
    )


@router.post(
    "/auth",
    responses={
        200: {"description": "Authenticated (no 2FA needed)"},
        202: {"description": "2FA challenge issued"},
        400: {"description": "Bad request body"},
        401: {"description": "Invalid credentials or missing bearer"},
        403: {"description": "Non-loopback origin"},
    },
)
async def auth_begin(
    request: Request,
    body: AuthBeginRequest,
    steam: SteamWorkerClient = Depends(get_steam_client_dep),  # noqa: B008
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    _log.info("platform.auth.began", platform="steam", username_present=True)

    try:
        result = await steam.auth_begin(body.username, body.password)
    except SteamWorkerError as e:
        await _update_platform_row_failure(pool, error=e.kind)
        _log.warning("platform.auth.failed", kind=e.kind)
        return JSONResponse(
            status_code=401,
            content={"detail": f"authentication failed: {e.kind}"},
        )
    except (IPCTimeoutError, WorkerDiedError, WorkerDisabledError) as e:
        _log.error("platform.auth.worker_unavailable", kind=type(e).__name__)
        return JSONResponse(status_code=503, content={"detail": "steam worker unavailable"})
    except PoolError as e:
        _log.error("platform.auth.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    if result.get("authenticated"):
        await _update_platform_row_success(
            pool, steam_id=int(result["steam_id"]), username=body.username
        )
        _log.info("platform.auth.completed", steam_id=result["steam_id"])
        return JSONResponse(
            status_code=200,
            content=AuthSuccessResponse(steam_id=int(result["steam_id"])).model_dump(),
        )

    # 2FA challenge — evict expired entries first (#95 item 3), then store
    # both the expiry and the username so auth_complete doesn't have to
    # recover it from the DB (#94).
    _sweep_expired_challenges()
    challenge_id = result["challenge_id"]
    _challenges[challenge_id] = _ChallengeState(
        expires_at_mono=time.monotonic() + CHALLENGE_TTL_SEC,
        username=body.username,
    )
    expires_at_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + CHALLENGE_TTL_SEC)
    )
    return JSONResponse(
        status_code=202,
        content=AuthChallengeResponse(
            challenge_id=challenge_id,
            challenge_type=result["challenge_type"],
            expires_at=expires_at_iso,
        ).model_dump(),
    )


@router.post("/auth/{challenge_id}")
async def auth_complete(
    challenge_id: str,
    body: AuthCompleteRequest,
    steam: SteamWorkerClient = Depends(get_steam_client_dep),  # noqa: B008
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    challenge = _challenges.get(challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="unknown challenge_id")
    if time.monotonic() > challenge["expires_at_mono"]:
        _challenges.pop(challenge_id, None)
        raise HTTPException(status_code=404, detail="challenge expired")

    try:
        result = await steam.auth_complete(challenge_id, body.code)
    except SteamWorkerError as e:
        _challenges.pop(challenge_id, None)
        try:
            await _update_platform_row_failure(pool, error=e.kind)
        except PoolError as db_e:
            # #93: don't let a DB failure during the failure-log mask the
            # original auth failure. Log + still return 401 for the auth.
            _log.error("platform.auth.db_unavailable", reason=str(db_e))
        return JSONResponse(
            status_code=401,
            content={"detail": f"authentication failed: {e.kind}"},
        )
    except (IPCTimeoutError, WorkerDiedError, WorkerDisabledError):
        return JSONResponse(status_code=503, content={"detail": "steam worker unavailable"})

    # Successful 2FA — recover the username from the in-memory challenge
    # state (#94 fix; was incorrectly read-from-DB which silently lost it
    # on first-ever auth) before clearing the entry.
    username = challenge["username"]
    _challenges.pop(challenge_id, None)
    try:
        await _update_platform_row_success(
            pool, steam_id=int(result["steam_id"]), username=username
        )
    except PoolError as e:
        # #93: mirror auth_begin's 503-on-DB-error contract.
        _log.error("platform.auth.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    # #95 item 5: log the 2FA-success path (parallel to auth_begin's
    # `platform.auth.completed` log on the no-2FA path).
    _log.info("platform.auth.completed_2fa", steam_id=result["steam_id"])
    return JSONResponse(
        status_code=200,
        content=AuthSuccessResponse(steam_id=int(result["steam_id"])).model_dump(),
    )


@router.get("/auth/status")
async def auth_status(
    steam: SteamWorkerClient = Depends(get_steam_client_dep),  # noqa: B008
) -> JSONResponse:
    try:
        result = await steam.auth_status()
    except (IPCTimeoutError, WorkerDiedError, WorkerDisabledError):
        return JSONResponse(
            status_code=200,
            content=AuthStatusResponse(
                authenticated=False,
                last_check_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ).model_dump(),
        )
    return JSONResponse(
        status_code=200,
        content=AuthStatusResponse(
            authenticated=bool(result.get("authenticated", False)),
            steam_id=result.get("steam_id"),
            last_check_at=result["last_check_at"],
        ).model_dump(),
    )
