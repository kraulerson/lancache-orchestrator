"""Agent /v1/epic/validate — disk-stat an Epic game's stored manifest against the
lancache cache. Parity with agent/routers/steam.py::steam_validate. STDLIB + the
agent-safe validator/platform modules only; MUST NOT import orchestrator.api.* /
orchestrator.db.*. No network, no auth — parses the manifest bytes it's given and
stats local cache files."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.platform.epic.manifest import EpicManifestError, chunk_path, parse_manifest
from orchestrator.validator.cache_key import cache_key, cache_path, epic_chunk_uri, slice_range_zero
from orchestrator.validator.disk_stat import validate_chunks_any

_log = structlog.get_logger(__name__)
router = APIRouter()


class EpicValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: int = Field(..., ge=0)
    version: str
    cdn_base: str
    raw_manifest_b64: str


def _classify(total: int, cached: int) -> str:
    if total == 0 or cached == total:
        return "cached"
    if cached == 0:
        return "missing"
    return "partial"


def _err(msg: str) -> dict[str, Any]:
    return {
        "chunks_total": 0,
        "chunks_cached": 0,
        "chunks_missing": 0,
        "outcome": "error",
        "versions": "",
        "error": msg,
    }


@router.post("/v1/epic/validate", status_code=status.HTTP_200_OK)
async def epic_validate(body: EpicValidateRequest, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    identifiers = settings.epic_cache_identifiers
    if not identifiers:
        return _err("no_epic_identifiers")
    try:
        manifest = parse_manifest(base64.b64decode(body.raw_manifest_b64))
    except (EpicManifestError, ValueError) as e:
        _log.warning(
            "epic_validate.parse_failed",
            app_id=body.app_id,
            reason=f"{type(e).__name__}: {e}"[:200],
        )
        return _err("manifest_parse_failed")

    cache_root = Path(settings.lancache_nginx_cache_path)
    slice_range = slice_range_zero(settings.cache_slice_size_bytes)
    levels = settings.cache_levels

    candidate_lists: list[list[Path]] = []
    seen: set[str] = set()
    for chunk in manifest.chunks:
        cp = chunk_path(chunk, manifest.version)
        if cp in seen:
            continue  # de-dupe identical chunks (same content -> same path)
        seen.add(cp)
        uri = epic_chunk_uri(cp, body.cdn_base)
        candidate_lists.append(
            [
                cache_path(cache_root, cache_key(ident, uri, slice_range), levels)
                for ident in identifiers
            ]
        )

    total = len(candidate_lists)
    if total == 0:
        return {
            "chunks_total": 0,
            "chunks_cached": 0,
            "chunks_missing": 0,
            "outcome": "cached",
            "versions": str(manifest.version),
            "error": None,
        }
    cached, _present = await validate_chunks_any(candidate_lists)
    return {
        "chunks_total": total,
        "chunks_cached": cached,
        "chunks_missing": total - cached,
        "outcome": _classify(total, cached),
        "versions": str(manifest.version),
        "error": None,
    }
