"""GET /api/v1/manual-downloads/{launcher} — list manually-downloaded game folders (#222).

The manually-downloaded games (GOG / Humble / Itch / Amazon — launchers with no
prefill automation) live in per-launcher folders on the lancache host, which only
the data-plane agent can see. This proxies the agent's listing so Game_shelf can
diff the owned library against what was actually downloaded. Read-only.
"""

from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

_log = structlog.get_logger(__name__)

# Same allowlist the agent enforces — a traversal-safe path component that may
# contain spaces/dots (Amazon Games, Itch.io) but never a '/'.
_LAUNCHER_RE = re.compile(r"^[A-Za-z0-9 ._-]+$")

router = APIRouter(prefix="/api/v1", tags=["manual-downloads"])


@router.get(
    "/manual-downloads/{launcher}",
    responses={
        200: {"description": "Folder listing {launcher, present, entries}"},
        400: {"description": "Invalid launcher"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Agent not configured or unavailable"},
    },
    summary="List manually-downloaded game folders for a launcher",
)
async def manual_downloads(
    launcher: str, request: Request, include_files: bool = False
) -> JSONResponse:
    if not _LAUNCHER_RE.match(launcher):
        return JSONResponse(content={"detail": "invalid launcher"}, status_code=400)
    client = getattr(request.app.state, "agent_client", None)
    if client is None:
        return JSONResponse(content={"detail": "agent not configured"}, status_code=503)
    try:
        result = await client.manual_downloads(launcher, include_files=include_files)
    except Exception as e:  # agent down / transport error — never 500
        _log.error("api.manual_downloads.agent_error", launcher=launcher, reason=str(e)[:200])
        return JSONResponse(content={"detail": "agent unavailable"}, status_code=503)
    return JSONResponse(content=result)
