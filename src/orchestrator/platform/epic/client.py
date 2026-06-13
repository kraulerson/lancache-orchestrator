"""EpicClient (F6) — token lifecycle + library/manifest facade for handlers.

Mirrors SteamWorkerClient's role in Deps, but is pure async httpx (no subprocess).
Caches a valid access token; refreshes from the persisted refresh token on demand
and re-persists a rotated refresh token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

import structlog

from orchestrator.platform.epic import library, manifest, oauth

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from orchestrator.core.settings import Settings
    from orchestrator.platform.epic.models import (
        AuthTokens,
        EpicLibraryItem,
        EpicManifest,
    )

_log = structlog.get_logger(__name__)

_R = TypeVar("_R")


class EpicNotAuthenticatedError(Exception):
    """No usable Epic session (no stored refresh token, or refresh rejected)."""


def _is_expired(expires_at: str, buffer_sec: int) -> bool:
    """True if the access token expires within ``buffer_sec``. Unparseable /
    missing expiry → False (keep using until a 401 forces a refresh)."""
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp <= datetime.now(UTC) + timedelta(seconds=buffer_sec)


class EpicClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tokens: AuthTokens | None = None

    async def _access_token(self, *, force: bool = False) -> str:
        buffer_sec = self._settings.epic_refresh_buffer_sec
        if (
            not force
            and self._tokens is not None
            and not _is_expired(self._tokens.expires_at, buffer_sec)
        ):
            return self._tokens.access_token
        path = str(self._settings.epic_session_path)
        # Prefer the in-memory refresh token (a rotating session), else the
        # persisted one. Refresh proactively when the access token is near expiry.
        rt = (self._tokens.refresh_token if self._tokens else None) or oauth.load_refresh_token(
            path
        )
        if not rt:
            raise EpicNotAuthenticatedError("no stored Epic refresh token")
        try:
            self._tokens = await oauth.refresh(rt, self._settings)
        except oauth.EpicAuthError as e:
            raise EpicNotAuthenticatedError(str(e)) from e
        if self._tokens.refresh_token and self._tokens.refresh_token != rt:
            oauth.save_refresh_token(path, self._tokens.refresh_token)
        return self._tokens.access_token

    async def library_enumerate(self) -> list[EpicLibraryItem]:
        async def call(token: str) -> list[EpicLibraryItem]:
            return await library.enumerate_library(token, self._settings)

        return await self._call_with_401_refresh(call)

    async def fetch_manifest(self, item: EpicLibraryItem) -> tuple[EpicManifest, str, str]:
        async def call(token: str) -> tuple[EpicManifest, str, str]:
            return await manifest.fetch_manifest(token, item, self._settings)

        return await self._call_with_401_refresh(call)

    async def _call_with_401_refresh(self, call: Callable[[str], Awaitable[_R]]) -> _R:
        """Run ``call`` with the access token; on a 401 (token revoked early, or
        an unparseable/missing expiry that defeated the proactive refresh) force a
        token refresh and retry ONCE — the documented 401-forces-refresh contract
        that was previously unimplemented (audit 2026-06-09)."""
        token = await self._access_token()
        try:
            return await call(token)
        except (library.EpicLibraryError, manifest.EpicManifestError) as e:
            if getattr(e, "status_code", None) != 401:
                raise
            _log.info("epic.forcing_refresh_on_401")
            token = await self._access_token(force=True)
            return await call(token)
