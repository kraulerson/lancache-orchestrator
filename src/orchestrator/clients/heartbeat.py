"""Uptime Kuma push heartbeats.

Kuma expects a heartbeat within each monitor's interval and marks the monitor DOWN
when none arrives — so a job that silently stops running is caught by absence, which
is the failure mode nothing else in this system detects.

Wire format is a GET: ``<url>?status=up|down&msg=<text>``. The URL is the secret, so
it lives in the environment (``ORCH_KUMA_PUSH_*``) and never in the repo. An unset
variable disables that heartbeat.

**Monitoring must never break the thing it monitors.** Every failure here is
swallowed: no URL configured, DNS gone, connection refused, a timeout, a 500 from
Kuma itself. A job's outcome is decided by the job, never by whether we managed to
tell anyone about it. If Kuma is unreachable its monitor goes DOWN for want of a
heartbeat, which is the correct signal anyway.
"""

from __future__ import annotations

import httpx
import structlog

_log = structlog.get_logger(__name__)

# Kuma stores the message and shows it on the monitor. An unbounded error blob does
# not belong in a URL, and the useful part of a failure is its tail.
MSG_MAX_CHARS = 200

TIMEOUT_SEC = 10.0


async def push(
    url: str | None,
    *,
    status: str,
    msg: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Send one heartbeat. Never raises.

    Args:
        url: the monitor's push URL, or None/blank to disable this heartbeat.
        status: ``"up"`` or ``"down"``.
        msg: short human-readable detail, truncated to ``MSG_MAX_CHARS``.
        transport: injected by tests; production passes nothing.
    """
    if url is None or not url.strip():
        return

    # Keep the tail: for an error the specific failure is at the end, not the start.
    trimmed = msg[-MSG_MAX_CHARS:] if len(msg) > MSG_MAX_CHARS else msg

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC, transport=transport) as client:
            await client.get(url.strip(), params={"status": status, "msg": trimmed})
    except Exception as exc:
        _log.warning("heartbeat.push_failed", status=status, error=str(exc)[:MSG_MAX_CHARS])
