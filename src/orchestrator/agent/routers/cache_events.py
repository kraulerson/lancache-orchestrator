"""Agent GET /v1/cache/eviction-events — cache-catcher's eviction events (Feature B).

cache-catcher appends one JSON line per SENT eviction alert to a file on the
``cache-catcher-events`` volume, mounted read-only here. The control plane polls
this endpoint and restarts its sweep pass at the newest event it has not acted on
(Karl's N1: nothing on the NAS calls the orchestrator). Read-only.

Tolerant by design: a missing file means no evictions yet, and a malformed or
half-written line (cache-catcher may be mid-append) is skipped, never a 500.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter()
_log = structlog.get_logger(__name__)

_MAX_EVENTS = 1000
_FIELDS = {"ts": str, "deletes": int, "window_s": int, "actor": str}
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _read_events(path: Path, since: str | None) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if (
            not isinstance(raw, dict)
            or not all(
                isinstance(raw.get(k), t) and not isinstance(raw.get(k), bool)
                for k, t in _FIELDS.items()
            )
            # The control plane turns ts into the sweep-pass boundary via
            # strftime; any other shape would yield NULL there.
            or not _TS.match(raw["ts"])
        ):
            skipped += 1
            continue
        event = {k: raw[k] for k in _FIELDS}
        if since is None or event["ts"] > since:
            events.append(event)
    if skipped:
        _log.warning("agent.eviction_events.lines_skipped", count=skipped)
    return events[-_MAX_EVENTS:]


@router.get("/v1/cache/eviction-events")
async def eviction_events(request: Request, since: str | None = None) -> dict[str, Any]:
    path = request.app.state.settings.eviction_events_path
    # A missing FILE means no evictions yet; a missing DIRECTORY means the
    # cache-catcher-events volume is not mounted, which must not look the same
    # (final-review I4) — the control plane logs the error and keeps polling.
    if not await asyncio.to_thread(path.parent.is_dir):
        _log.error("agent.eviction_events.not_mounted", directory=str(path.parent))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="eviction events directory not mounted",
        )
    events = await asyncio.to_thread(_read_events, path, since)
    return {"events": events}
