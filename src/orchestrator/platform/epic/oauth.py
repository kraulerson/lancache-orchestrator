"""Epic OAuth (F6): authorization_code + refresh_token grants, token persistence.

Pure async httpx. The client_id/secret are the public legendary launcher creds.
Access/refresh tokens are secret — never put them in log event fields. The refresh
token persists as JSON at the operator-configured ``epic_session_path`` (0600),
mirroring ``steam_session_path``.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from orchestrator.platform.epic.models import AuthTokens

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)


class EpicAuthError(Exception):
    """Epic OAuth exchange/refresh failed."""


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject httpx.MockTransport. None -> real network."""
    return None


def _client(settings: Settings) -> httpx.AsyncClient:
    transport = _build_transport()
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(30.0, connect=10.0),
        "headers": {"User-Agent": settings.epic_user_agent},
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def _to_tokens(d: dict[str, Any]) -> AuthTokens:
    return AuthTokens(
        access_token=d["access_token"],
        refresh_token=d.get("refresh_token", ""),
        account_id=str(d.get("account_id", "")),
        display_name=str(d.get("displayName", d.get("account_id", "unknown"))),
        expires_at=str(d.get("expires_at", "")),
    )


async def _grant(data: dict[str, str], settings: Settings, *, what: str) -> AuthTokens:
    async with _client(settings) as client:
        resp = await client.post(
            settings.epic_token_url,
            auth=(settings.epic_client_id, settings.epic_client_secret),
            data=data,
        )
    if resp.status_code != 200:
        # The error body can echo the rejected token — log only status + errorCode.
        code = ""
        with contextlib.suppress(Exception):  # body may not be JSON
            code = str(resp.json().get("errorCode", ""))
        _log.warning("epic.oauth.failed", what=what, status=resp.status_code, error_code=code)
        raise EpicAuthError(f"epic {what} failed: HTTP {resp.status_code} {code}")
    return _to_tokens(resp.json())


async def exchange_code(code: str, settings: Settings) -> AuthTokens:
    return await _grant(
        {"grant_type": "authorization_code", "code": code, "token_type": "eg1"},
        settings,
        what="exchange_code",
    )


async def refresh(refresh_token: str, settings: Settings) -> AuthTokens:
    return await _grant(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "token_type": "eg1"},
        settings,
        what="refresh",
    )


def save_refresh_token(session_path: str, refresh_token: str) -> None:
    """Persist the refresh token as JSON at ``session_path`` (0600)."""
    p = Path(session_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    # O_NOFOLLOW: refuse to write through a symlink planted at the token path
    # (TOCTOU / arbitrary-file-truncation guard — adversarial review).
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(str(p), flags, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.chmod(str(p), 0o600)


def load_refresh_token(session_path: str) -> str | None:
    p = Path(session_path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    rt = data.get("refresh_token")
    return rt if isinstance(rt, str) and rt else None
