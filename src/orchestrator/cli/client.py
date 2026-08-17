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


# Bound on rendered detail text: an error body can be an arbitrarily large HTML
# page from a reverse proxy, and this text lands on the operator's terminal.
_DETAIL_MAX = 200


def _error_detail(resp: httpx.Response) -> str:
    """Human-readable detail from an error response, or ``""`` if the server
    sent nothing usable (issue #265).

    The RAW value is tested before any ``str()`` coercion. ``str(None)`` is the
    truthy literal ``"None"``, so a ``{"detail": null}`` body would otherwise
    render as ``HTTP 404: None``; a whitespace-only value stays truthy the same
    way while displaying as nothing after the colon. Both report empty here so
    the caller can substitute a message that actually says something.

    An explicit ``{"detail": null}`` returns ``""`` rather than falling back to
    the body text: it is a well-formed API error envelope that simply carries
    no explanation, and echoing the raw JSON back gives the operator nothing to
    act on.
    """
    parsed: Any
    try:
        parsed = resp.json()
    except Exception:
        parsed = None

    if isinstance(parsed, dict) and "detail" in parsed:
        raw = parsed["detail"]
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw.strip()
        # A structured detail — the API's RequestValidationError handler returns
        # `detail` as a LIST of per-field errors (e.g. the #263 out-of-range
        # game_id 400). Render it; dropping it would hide the real failure.
        return str(raw).strip()[:_DETAIL_MAX]

    # Not a JSON object, or an object with no `detail`: fall back to the body.
    return resp.text.strip()[:_DETAIL_MAX]


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
            raise AuthError(_error_detail(resp) or "authentication failed — check ORCH_TOKEN")
        if not (200 <= resp.status_code < 300) and resp.status_code not in ok_extra:
            detail = _error_detail(resp)
            if not detail:
                # Static text plus the request we made — never anything the
                # server chose. NOT resp.reason_phrase: httpx decodes it as
                # ASCII with errors="ignore" and h11 accepts any non-CRLF
                # bytes, so ANSI escapes could reach the operator's terminal.
                # An earlier attempt used it and was reverted for that (#265).
                #
                # The two phrases are different diagnoses: nothing at all points
                # at a wrong --url/ORCH_API_URL or a proxy answering for a route
                # the orchestrator never saw, while a body carrying no `detail`
                # means the API did answer but explained nothing.
                #
                # The synthesized text lives ONLY in this human-facing message.
                # ApiError carries no structured detail field, so no consumer
                # can branch on a string the server did not actually send.
                missing = "no response body" if not resp.content else "no error detail in response"
                detail = f"{missing} ({method.upper()} {path})"
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
