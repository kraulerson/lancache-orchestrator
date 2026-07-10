"""Agent GET /v1/manual-downloads/{launcher} — list manually-downloaded game folders (#222).

Karl stores games he downloaded by hand (GOG / Humble / Itch / Amazon — launchers
with no prefill automation) in per-launcher folders under the cache root, e.g.
``<manual_downloads_cache_path>/GOG/<game>``. This endpoint lists those folder
names so the control plane can diff them against the owned library and report
which owned games were never downloaded. Read-only.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

router = APIRouter()

# The launcher is a single path component (a folder name on disk, e.g. "GOG").
# Restricting it to alnum / '_' / '-' means it can contain NO '.' or '/', so it
# can never form '..' or escape the cache root — path-traversal is impossible by
# construction (defense-in-depth resolve-check below regardless).
_LAUNCHER_RE = re.compile(r"^[A-Za-z0-9 ._-]+$")


class ManualDownloadsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    launcher: str
    present: bool
    entries: list[str]


@router.get("/v1/manual-downloads/{launcher}")
async def manual_downloads(
    launcher: str, request: Request, include_files: bool = False
) -> ManualDownloadsResponse:
    if not _LAUNCHER_RE.match(launcher):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid launcher")
    settings = request.app.state.settings
    root = settings.manual_downloads_cache_path.resolve()
    target = (root / launcher).resolve()
    # Defense-in-depth: the sanitized launcher can't traverse, but re-assert the
    # resolved target is a direct child of the cache root before touching disk.
    if target.parent != root:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid launcher")
    if not target.is_dir():
        return ManualDownloadsResponse(launcher=launcher, present=False, entries=[])
    # Dir-per-game launchers (GOG/Amazon) list folders only. File-based launchers
    # (Humble/Itch — loose installers) opt into files via include_files. Always skip
    # lancache control entries (!downloading / !orphaned) and dotfiles.
    entries = sorted(
        e.name
        for e in target.iterdir()
        if (e.is_dir() or (include_files and e.is_file())) and not e.name.startswith(("!", "."))
    )
    return ManualDownloadsResponse(launcher=launcher, present=True, entries=entries)
