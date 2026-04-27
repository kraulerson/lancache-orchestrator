"""Three pure-ASGI middlewares for BL5 (spec §5).

Pure-ASGI (not BaseHTTPMiddleware) chosen because:
  1. BodySizeCap needs receive() interception for streaming bodies
  2. Consistency across all three middlewares
  3. BaseHTTPMiddleware has documented BackgroundTasks/exception-handler
     issues (FastAPI release notes around 0.106).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from orchestrator.api.dependencies import (
    AUTH_EXEMPT_PREFIXES,
    BODY_SIZE_CAP_BYTES,
    LOOPBACK_ONLY_PATTERNS,
)
from orchestrator.core.logging import request_context
from orchestrator.core.settings import get_settings

ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[Any]],
        Callable[[Any], Awaitable[None]],
    ],
    Awaitable[None],
]
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

_log = structlog.get_logger(__name__)

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------
# CorrelationIdMiddleware (spec §5.2)
# ----------------------------------------------------------------------


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        cid_bytes = headers.get(b"x-correlation-id", b"")
        cid_in = cid_bytes.decode("ascii", errors="ignore")
        cid = cid_in if _UUID4_RE.match(cid_in) else str(uuid.uuid4())

        with request_context(correlation_id=cid):
            log = structlog.get_logger()
            t0 = time.perf_counter()
            log.info(
                "api.request.received",
                method=scope["method"],
                path=scope["path"],
                correlation_id=cid,
            )

            async def send_with_cid(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    response_headers = list(message.get("headers", []))
                    response_headers.append((b"x-correlation-id", cid.encode("ascii")))
                    message = {**message, "headers": response_headers}
                await send(message)

            try:
                await self.app(scope, receive, send_with_cid)
            finally:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                log.info(
                    "api.request.completed",
                    duration_ms=duration_ms,
                    correlation_id=cid,
                )


# ----------------------------------------------------------------------
# BodySizeCapMiddleware (spec §5.3)
# ----------------------------------------------------------------------


class _BodyTooLargeError(Exception):
    """Raised internally to signal cap exhaustion; converted to 413 response."""


class BodySizeCapMiddleware:
    def __init__(self, app: ASGIApp, cap: int = BODY_SIZE_CAP_BYTES) -> None:
        self.app = app
        self.cap = cap

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        cl_bytes = headers.get(b"content-length")

        # Path 1: Content-Length present
        if cl_bytes is not None:
            try:
                cl = int(cl_bytes)
            except ValueError:
                cl = 0
            if cl > self.cap:
                _log.error(
                    "api.body_size_cap_exceeded",
                    path=scope["path"],
                    content_length=cl,
                    cap=self.cap,
                )
                await self._send_413(send)
                return

        # Path 2: streaming — track bytes via wrapped receive()
        bytes_received = 0

        async def receive_with_cap() -> dict[str, Any]:
            nonlocal bytes_received
            msg = await receive()
            if msg["type"] == "http.request":
                body = msg.get("body", b"")
                bytes_received += len(body)
                if bytes_received > self.cap:
                    raise _BodyTooLargeError()
            return msg

        try:
            await self.app(scope, receive_with_cap, send)
        except _BodyTooLargeError:
            _log.error(
                "api.body_size_cap_exceeded",
                path=scope["path"],
                bytes_received=bytes_received,
                cap=self.cap,
            )
            await self._send_413(send)

    @staticmethod
    async def _send_413(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"request body exceeds 32 KiB cap"}',
            }
        )


# ----------------------------------------------------------------------
# BearerAuthMiddleware (spec §5.4)
# ----------------------------------------------------------------------


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        method: str = scope["method"]

        # Skip preflight
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Skip exempt paths
        if any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("ascii", errors="ignore")

        if not auth_header:
            _log.warning("api.auth.rejected", reason="missing_header", path=path)
            await self._send_401(send)
            return

        if not auth_header.startswith("Bearer "):
            _log.warning("api.auth.rejected", reason="malformed_header", path=path)
            await self._send_401(send)
            return

        token = auth_header[len("Bearer ") :].strip()
        if not token:
            _log.warning("api.auth.rejected", reason="malformed_header", path=path)
            await self._send_401(send)
            return

        settings = get_settings()
        expected = settings.orchestrator_token.get_secret_value()
        if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            sha = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
            # Field name avoids the words "token", "auth", "secret", "bearer"
            # because ID3's _redact_sensitive_values would auto-redact them.
            # The sha256 prefix is intentionally non-sensitive (8 hex of a
            # one-way hash; cannot reconstruct the original token) so we
            # need it to actually appear in the log.
            _log.warning(
                "api.auth.rejected",
                reason="bad_token",
                path=path,
                rejection_fingerprint=sha,
            )
            await self._send_401(send)
            return

        # OQ2: 127.0.0.1 enforcement on POST /api/v1/platforms/{name}/auth
        if any(p.match(path) for p in LOOPBACK_ONLY_PATTERNS):
            client_info = scope.get("client")
            client_host = client_info[0] if client_info else None
            if client_host != "127.0.0.1":
                _log.warning(
                    "api.auth.rejected",
                    reason="non_loopback",
                    path=path,
                    client_host=client_host,
                )
                await self._send_403(send)
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="orchestrator"'),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"unauthorized"}',
            }
        )

    @staticmethod
    async def _send_403(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"forbidden: loopback only"}',
            }
        )
