"""POST /api/v1/platforms/steam/auth* — Steam authentication (BL10 / F1)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Literal

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

# In-memory challenge state: challenge_id -> expires_at_monotonic
# Per F1 D11: 5-min TTL; server restart invalidates (acceptable).
_challenge_expiries: dict[str, float] = {}


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

    # 2FA challenge
    challenge_id = result["challenge_id"]
    _challenge_expiries[challenge_id] = time.monotonic() + CHALLENGE_TTL_SEC
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
    expires_at_mono = _challenge_expiries.get(challenge_id)
    if expires_at_mono is None:
        raise HTTPException(status_code=404, detail="unknown challenge_id")
    if time.monotonic() > expires_at_mono:
        _challenge_expiries.pop(challenge_id, None)
        raise HTTPException(status_code=404, detail="challenge expired")

    try:
        result = await steam.auth_complete(challenge_id, body.code)
    except SteamWorkerError as e:
        _challenge_expiries.pop(challenge_id, None)
        await _update_platform_row_failure(pool, error=e.kind)
        return JSONResponse(
            status_code=401,
            content={"detail": f"authentication failed: {e.kind}"},
        )
    except (IPCTimeoutError, WorkerDiedError, WorkerDisabledError):
        return JSONResponse(status_code=503, content={"detail": "steam worker unavailable"})

    _challenge_expiries.pop(challenge_id, None)
    # Read the previous row's config to recover the username
    prev_config_row = await pool.read_one("SELECT config FROM platforms WHERE name='steam'")
    prev_username = ""
    if prev_config_row and prev_config_row["config"]:
        try:
            prev_username = (json.loads(prev_config_row["config"]) or {}).get("username", "")
        except (json.JSONDecodeError, TypeError):
            prev_username = ""
    await _update_platform_row_success(
        pool, steam_id=int(result["steam_id"]), username=prev_username
    )
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
