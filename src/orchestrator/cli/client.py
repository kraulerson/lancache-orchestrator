"""F11 — thin synchronous HTTP client for the orchestrator REST API.

Exit-code-bearing exceptions: ApiUnreachableError -> 2, AuthError -> 3, ApiError -> 1
(``main``/``base`` map them). Mirrors the project's ``_build_transport()``
MockTransport seam used in ``platform/epic`` (here the seam is the injectable
``_transport`` attribute set by tests).
"""

from __future__ import annotations

from typing import Any

import httpx


class OrchClientError(Exception):
    """Base for CLI client errors; carries the process exit code."""

    exit_code = 1


class ApiUnreachableError(OrchClientError):
    exit_code = 2


class AuthError(OrchClientError):
    exit_code = 3


class ApiError(OrchClientError):
    exit_code = 1


class OrchClient:
    """Synchronous orchestrator API client. Click is sync; ``httpx.Client`` fits."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport: httpx.BaseTransport | None = None  # test seam

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        ok_extra: tuple[int, ...] = (),
    ) -> Any:
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "timeout": httpx.Timeout(30.0, connect=5.0),
            "headers": {"Authorization": f"Bearer {self._token}"},
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            with httpx.Client(**kwargs) as client:
                resp = client.request(method, path, params=params, json=json)
        except (httpx.TransportError, httpx.InvalidURL) as e:
            # TransportError is the base of every connect/read/write/pool/protocol
            # failure (incl. a server-disconnect mid-deploy restart). InvalidURL is
            # raised by the Client(base_url=...) constructor for a malformed --url
            # (e.g. a stray control char) and is NOT a TransportError — catch it
            # too so a fat-fingered --url is a clean exit 2, not a raw traceback.
            raise ApiUnreachableError(f"orchestrator API not reachable at {self._base_url}") from e
        if resp.status_code == 401:
            # Surface the server's detail when present (e.g. a wrong Steam/Epic
            # credential during `auth`), not the misleading hardcoded ORCH_TOKEN
            # hint — the bearer token was accepted in that case (UAT-11 S11-E-03).
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except Exception:
                detail = ""
            raise AuthError(detail or "authentication failed — check ORCH_TOKEN")
        if not (200 <= resp.status_code < 300) and resp.status_code not in ok_extra:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except Exception:
                detail = resp.text[:200]
            raise ApiError(f"HTTP {resp.status_code}: {detail}")
        if resp.content:
            return resp.json()
        return None

    def get(self, path: str, **params: Any) -> Any:
        # Drop None-valued params so optional filters are omitted cleanly.
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", path, params=clean or None)

    def get_health(self) -> Any:
        """GET /health, tolerating the degraded representation.

        ``/health`` returns ``503`` *with a body* when degraded; that is the
        intended representation, not an error — return the body either way so
        the caller renders the degraded state instead of exiting non-zero.
        """
        return self._request("GET", "/api/v1/health", ok_extra=(503,))

    def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return self._request("POST", path, params=params, json=json)

    def delete(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return self._request("DELETE", path, json=json)
