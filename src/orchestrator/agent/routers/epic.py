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

from orchestrator.agent._paths import under_cache_root
from orchestrator.platform.epic.manifest import EpicManifestError, chunk_path, parse_manifest
from orchestrator.validator.cache_key import cache_key, cache_path, epic_chunk_uri, slice_range_zero
from orchestrator.validator.disk_stat import purge_chunks, validate_chunks_any

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


def _epic_candidate_paths(
    settings: Any, cdn_base: str, raw_manifest_bytes: bytes
) -> tuple[str, list[list[Path]]]:
    """Parse an Epic manifest and derive, per unique chunk, the list of candidate
    nginx cache paths — one per configured cache identifier (Epic content is cached
    under one of several per-CDN-host identifiers). Shared by ``/v1/epic/validate``
    and ``/v1/epic/purge`` (DRY). Returns ``(manifest_version, candidate_lists)``.

    Raises ``EpicManifestError`` / ``ValueError`` on an unparseable manifest.
    Requires ``settings.epic_cache_identifiers`` to be non-empty (caller checks).
    """
    identifiers = settings.epic_cache_identifiers
    manifest = parse_manifest(raw_manifest_bytes)
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
        uri = epic_chunk_uri(cp, cdn_base)
        candidate_lists.append(
            [
                cache_path(cache_root, cache_key(ident, uri, slice_range), levels)
                for ident in identifiers
            ]
        )
    return str(manifest.version), candidate_lists


@router.post("/v1/epic/validate", status_code=status.HTTP_200_OK)
async def epic_validate(body: EpicValidateRequest, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    if not settings.epic_cache_identifiers:
        return _err("no_epic_identifiers")
    try:
        version, candidate_lists = _epic_candidate_paths(
            settings, body.cdn_base, base64.b64decode(body.raw_manifest_b64)
        )
    except (EpicManifestError, ValueError) as e:
        _log.warning(
            "epic_validate.parse_failed",
            app_id=body.app_id,
            reason=f"{type(e).__name__}: {e}"[:200],
        )
        return _err("manifest_parse_failed")

    total = len(candidate_lists)
    if total == 0:
        return {
            "chunks_total": 0,
            "chunks_cached": 0,
            "chunks_missing": 0,
            "outcome": "cached",
            "versions": version,
            "error": None,
        }
    cached, _present = await validate_chunks_any(candidate_lists)
    return {
        "chunks_total": total,
        "chunks_cached": cached,
        "chunks_missing": total - cached,
        "outcome": _classify(total, cached),
        "versions": version,
        "error": None,
    }


class EpicPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: int = Field(..., ge=0)
    version: str
    cdn_base: str
    raw_manifest_b64: str


@router.post("/v1/epic/purge", status_code=status.HTTP_200_OK)
async def epic_purge(body: EpicPurgeRequest, request: Request) -> dict[str, int]:
    """Delete an Epic game's cached chunk files (F18). Enumerates the SAME
    candidate paths as ``/v1/epic/validate`` (via ``_epic_candidate_paths``) and
    unlinks every present candidate — Epic content is cached under one of several
    per-CDN-host identifiers, so we delete all candidates and let the ones that
    aren't on disk no-op.

    Idempotent + best-effort: an unparseable manifest or empty identifiers yields
    ``{deleted: 0}`` (logged), never a 500 — the control handler already
    guarantees a manifest exists. The path-safety guard bounds every unlink to
    inside the cache root. The control plane sets ``status='validation_failed'``
    afterward so the game re-prefills (ADR-0015 — purge is reversible)."""
    settings = request.app.state.settings
    if not settings.epic_cache_identifiers:
        _log.warning("agent.epic_purge.no_identifiers", app_id=body.app_id)
        return {"deleted": 0, "failed": 0, "bytes_freed": 0}
    try:
        _version, candidate_lists = _epic_candidate_paths(
            settings, body.cdn_base, base64.b64decode(body.raw_manifest_b64)
        )
    except (EpicManifestError, ValueError) as e:
        _log.warning(
            "agent.epic_purge.parse_failed",
            app_id=body.app_id,
            reason=f"{type(e).__name__}: {e}"[:200],
        )
        return {"deleted": 0, "failed": 0, "bytes_freed": 0}

    paths = [p for cands in candidate_lists for p in cands]
    safe = under_cache_root(Path(settings.lancache_nginx_cache_path), paths)
    deleted, failed, freed = await purge_chunks(safe)
    _log.info(
        "agent.epic_purge",
        app_id=body.app_id,
        deleted=deleted,
        failed=failed,
        bytes_freed=freed,
    )
    return {"deleted": deleted, "failed": failed, "bytes_freed": freed}
