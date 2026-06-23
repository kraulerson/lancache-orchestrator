"""Agent /v1/steam/* — drives the host SteamPrefill binary via SteamPrefillDriver."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.agent.manifest_locator import list_prefilled_app_ids, locate_manifest_bins
from orchestrator.agent.manifest_parser import parse_chunk_shas
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)
from orchestrator.validator.disk_stat import validate_chunks

_log = structlog.get_logger(__name__)

router = APIRouter()


class SteamPrefillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_ids: list[int] = Field(..., min_length=1)
    force: bool = False


def _validate_app_ids(app_ids: list[int]) -> None:
    if any(a < 0 for a in app_ids):
        raise HTTPException(status_code=422, detail="app_ids must be non-negative")


@router.post("/v1/steam/prefill", status_code=status.HTTP_202_ACCEPTED)
async def start_prefill(body: SteamPrefillRequest, request: Request) -> dict[str, str]:
    _validate_app_ids(body.app_ids)
    driver = request.app.state.prefill_driver
    store = request.app.state.agent_jobs
    job_id = store.create()

    async def _run() -> None:
        try:
            result = await driver.prefill_apps(body.app_ids, force=body.force)
            store.set_done(job_id, {"ok": result.ok, "raw": result.raw})
        except Exception as e:  # record, never crash the loop
            store.set_failed(job_id, f"{type(e).__name__}: {e}"[:200])

    # Hold a strong reference so the fire-and-forget task is not GC'd mid-flight
    # (mirrors the /v1/pull background-task set + discard-on-done pattern).
    bg_tasks = request.app.state.agent_bg_tasks
    task = asyncio.create_task(_run())
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return {"job_id": job_id}


@router.get("/v1/steam/prefill/{job_id}")
async def get_prefill(job_id: str, request: Request) -> dict[str, Any]:
    snap: dict[str, Any] | None = request.app.state.agent_jobs.get(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="job not found")
    return snap


@router.get("/v1/steam/downloaded-state")
async def downloaded_state(request: Request) -> dict[str, list[int]]:
    state = request.app.state.prefill_driver.downloaded_state()
    return {str(k): v for k, v in state.items()}


@router.get("/v1/steam/auth-status")
async def auth_status(request: Request) -> dict[str, Any]:
    st = request.app.state.prefill_driver.auth_status()
    return {"ok": st.ok, "reason": st.reason}


@router.get("/v1/steam/prefilled-apps")
async def prefilled_apps(request: Request) -> dict[str, list[int]]:
    """Distinct app_ids with a cached manifest (real game app_ids from the .bin
    filenames) — the enumeration source for library_sync."""
    manifest_cache = Path(request.app.state.settings.steam_manifest_cache_dir)
    return {"app_ids": list_prefilled_app_ids(cache_root=manifest_cache)}


class SteamValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: int = Field(..., ge=0)


def _classify(total: int, cached: int) -> str:
    # total == 0 here means the located manifests contained no chunks —
    # nothing to cache, so the app is up to date ('cached'). The genuinely
    # no-manifest case returns 'error' before reaching classification.
    if total == 0:
        return "cached"
    if cached == total:
        return "cached"
    if cached == 0:
        return "missing"
    return "partial"


@router.post("/v1/steam/validate")
async def steam_validate(body: SteamValidateRequest, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    cache_root = Path(settings.lancache_nginx_cache_path)
    manifest_cache = Path(settings.steam_manifest_cache_dir)

    bins = locate_manifest_bins(body.app_id, cache_root=manifest_cache)
    if not bins:
        return {
            "chunks_total": 0,
            "chunks_cached": 0,
            "chunks_missing": 0,
            "outcome": "error",
            "versions": "",
            "error": "no_manifest_in_cache",
        }

    slice_range = slice_range_zero(settings.cache_slice_size_bytes)
    identifier = settings.steam_cache_identifier
    levels = settings.cache_levels

    seen: set[tuple[int, str]] = set()
    paths: list[Path] = []
    versions: list[str] = []
    parsed_ok = 0
    for binpath in bins:
        # filename is {app}_{app}_{depot}_{gid}.bin. A corrupt/foreign .bin (a
        # non-numeric depot field, a deleted/unreadable file) must NOT 500 the
        # whole request — skip it and keep validating the rest (COR-1).
        try:
            parts = binpath.stem.split("_")
            depot_id = int(parts[2])
            gid = parts[3]
            chunk_shas = parse_chunk_shas(binpath.read_bytes())
        except (ValueError, IndexError, OSError) as e:
            _log.warning(
                "steam_validate.bin_skipped",
                bin=binpath.name,
                reason=f"{type(e).__name__}: {e}"[:200],
            )
            continue
        parsed_ok += 1
        versions.append(f"{depot_id}:{gid}")
        for sha in chunk_shas:
            key = (depot_id, sha)
            if key in seen:
                continue
            seen.add(key)
            uri = steam_chunk_uri(depot_id, sha)
            h = cache_key(identifier, uri, slice_range)
            paths.append(cache_path(cache_root, h, levels))

    if parsed_ok == 0:
        # Manifests existed but none could be parsed — a genuine error, not a
        # spurious 'cached' (which _classify would return for an empty path set).
        return {
            "chunks_total": 0,
            "chunks_cached": 0,
            "chunks_missing": 0,
            "outcome": "error",
            "versions": "",
            "error": "manifest_parse_failed",
        }

    cached, missing = await validate_chunks(paths)
    total = len(paths)
    return {
        "chunks_total": total,
        "chunks_cached": cached,
        "chunks_missing": missing,
        "outcome": _classify(total, cached),
        "versions": ",".join(sorted(versions)),
        "error": None,
    }
