"""Agent /v1/steam/* — drives the host SteamPrefill binary via SteamPrefillDriver."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.agent.manifest_archive import sync_manifests_to_archive
from orchestrator.agent.manifest_locator import list_prefilled_app_ids, locate_manifest_bins
from orchestrator.agent.manifest_parser import parse_chunk_shas, parse_shas
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)
from orchestrator.validator.disk_stat import validate_chunks_scoped

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
    settings = request.app.state.settings
    store = request.app.state.agent_jobs
    job_id = store.create()

    async def _run() -> None:
        try:
            result = await driver.prefill_apps(body.app_ids, force=body.force)
            if result.ok:
                # A successful prefill always writes its manifest(s) to the HOME
                # cache, so a MISSING live cache dir means SteamPrefill's HOME and
                # steam_prefill_live_cache_dir have drifted apart — the capture
                # would silently no-op and false-Partial badges would silently
                # return. Make that loud (UAT-13 F2b). (The driver pins HOME from
                # this same setting, so this should never fire — it's the canary.)
                live_v1 = Path(settings.steam_prefill_live_cache_dir) / "v1"
                if not live_v1.is_dir():
                    _log.warning(
                        "steam_prefill.live_cache_missing",
                        job_id=job_id,
                        live_cache=str(live_v1),
                        hint="HOME/.cache path mismatch; manifests NOT captured — check agent HOME",
                    )
                # Capture the manifest(s) SteamPrefill just wrote to its HOME
                # cache (the agent runs it with HOME=/tmp) into the durable
                # archive. The periodic archive-sync only reads the host cache, so
                # without this an agent-driven (force-)prefill's manifest is never
                # archived and validate falls back to a stale older manifest — the
                # false-Partial root cause. The run is finished so settle_seconds=0.
                # Synchronous: it's a bounded, fast copy of the few new .bin
                # manifests this prefill produced (not the whole library), so it
                # isn't worth offloading. A capture failure must never fail the job.
                try:
                    copied = sync_manifests_to_archive(
                        Path(settings.steam_prefill_live_cache_dir),
                        Path(settings.steam_manifest_archive_dir),
                        settle_seconds=0.0,
                    )
                    _log.info("steam_prefill.manifests_captured", job_id=job_id, copied=copied)
                except Exception as e:
                    _log.warning(
                        "steam_prefill.capture_failed",
                        job_id=job_id,
                        reason=f"{type(e).__name__}: {e}"[:200],
                    )
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
    s = request.app.state.settings
    roots = [Path(s.steam_manifest_cache_dir), Path(s.steam_manifest_archive_dir)]
    return {"app_ids": list_prefilled_app_ids(cache_roots=roots)}


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
    roots = [Path(settings.steam_manifest_cache_dir), Path(settings.steam_manifest_archive_dir)]

    # Pin manifest selection to the gid SteamPrefill actually prefilled for this
    # app (its own downloaded record), so validate measures the CURRENT version
    # rather than the newest manifest on disk — which can be a stale older build
    # and is the cause of false-Partial badges. Per-depot fallback to newest-by-
    # mtime when there's no record; tolerant of a missing/unreadable driver/file.
    try:
        state = request.app.state.prefill_driver.downloaded_state()
        prefilled_gids = {str(g) for g in state.get(body.app_id, [])}
    except Exception:
        prefilled_gids = set()

    bins = locate_manifest_bins(
        body.app_id, cache_roots=roots, prefilled_gids=prefilled_gids or None
    )
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
    depot_paths: dict[int, list[Path]] = {}
    versions: list[str] = []
    parsed_ok = 0
    for binpath in bins:
        # filename is {app}_{app}_{depot}_{gid}.{bin,shas}. A corrupt/foreign
        # manifest (a non-numeric depot field, a deleted/unreadable file) must
        # NOT 500 the whole request — skip it and keep validating the rest
        # (COR-1). .shas is the fetcher's sidecar (one SHA per line); .bin is
        # SteamPrefill's protobuf — same {app}_{app}_{depot}_{gid} field layout.
        try:
            parts = binpath.stem.split("_")
            depot_id = int(parts[2])
            gid = parts[3]
            if binpath.suffix == ".shas":
                chunk_shas = parse_shas(binpath.read_text())
            else:
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
        dpaths = depot_paths.setdefault(depot_id, [])
        for sha in chunk_shas:
            key = (depot_id, sha)
            if key in seen:
                continue
            seen.add(key)
            uri = steam_chunk_uri(depot_id, sha)
            h = cache_key(identifier, uri, slice_range)
            dpaths.append(cache_path(cache_root, h, levels))

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

    # Depot-scoping: SteamPrefill only prefills the operator's selected
    # language/OS depots, but the located manifest set can include extra depots
    # (other languages / optional content) the fetcher mapped but whose chunks
    # were never downloaded. A depot with NO chunk files on disk (present == 0)
    # was never prefilled, so it must NOT count against the game — otherwise
    # multi-language titles are perpetually 'partial'.
    #
    # We gate exclusion on `present`, NOT on `cached`: a depot whose files EXIST
    # but are unreadable (mode-000, #76/#128) or empty has present > 0 and is
    # KEPT, so that corruption stays visible as a gap instead of being silently
    # dropped as "never prefilled". (A depot fully evicted to 0 files on disk is
    # indistinguishable from never-prefilled and is excluded — accepted: whole-
    # depot eviction-to-zero is rare under per-file LRU.)
    total = 0
    cached = 0
    included = 0
    excluded: list[int] = []
    for depot_id, dpaths in sorted(depot_paths.items()):
        if not dpaths:
            continue
        d_cached, d_present = await validate_chunks_scoped(dpaths)
        if d_present == 0:
            excluded.append(depot_id)
            continue
        total += len(dpaths)
        cached += d_cached
        included += 1

    if excluded:
        _log.info(
            "steam_validate.depots_excluded",
            app_id=body.app_id,
            excluded=excluded,
            included=included,
        )

    if included == 0:
        # No depot has any cached chunks. If there were chunks to cache at all
        # the app is genuinely not cached ('missing'); if the manifests held no
        # chunks there's nothing to cache ('cached', matching _classify).
        union_total = sum(len(p) for p in depot_paths.values())
        return {
            "chunks_total": union_total,
            "chunks_cached": 0,
            "chunks_missing": union_total,
            "outcome": "missing" if union_total else "cached",
            "versions": ",".join(sorted(versions)),
            "error": None,
        }

    return {
        "chunks_total": total,
        "chunks_cached": cached,
        "chunks_missing": total - cached,
        "outcome": _classify(total, cached),
        "versions": ",".join(sorted(versions)),
        "error": None,
    }
