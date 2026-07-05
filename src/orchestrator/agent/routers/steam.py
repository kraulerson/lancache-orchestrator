"""Agent /v1/steam/* — drives the host SteamPrefill binary via SteamPrefillDriver."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.agent._paths import under_cache_root
from orchestrator.agent.manifest_archive import sync_manifests_to_archive
from orchestrator.agent.manifest_locator import list_prefilled_app_ids, locate_manifest_bins
from orchestrator.agent.manifest_parser import parse_chunk_shas, parse_shas
from orchestrator.platform.steam.selection_file import reconcile_selection
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)
from orchestrator.validator.disk_stat import purge_chunks, validate_chunks_scoped

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


@router.post("/v1/steam/fetch-manifests", status_code=status.HTTP_202_ACCEPTED)
async def start_fetch_manifests(request: Request) -> dict[str, str]:
    fetcher = request.app.state.manifest_fetcher
    store = request.app.state.agent_jobs
    inflight = getattr(request.app.state, "fetch_manifests_job", None)
    if inflight is not None:
        snap = store.get(inflight)
        if snap is not None and snap["state"] == "running":
            return {"job_id": inflight}
    job_id = store.create()
    request.app.state.fetch_manifests_job = job_id

    async def _run() -> None:
        try:
            result = await asyncio.to_thread(fetcher.fetch_all)
            store.set_done(
                job_id,
                {
                    "fetched": result.fetched,
                    "skipped": result.skipped,
                    "failed": result.failed,
                    "apps": result.apps,
                },
            )
        except Exception as e:  # record, never crash the loop
            store.set_failed(job_id, f"{type(e).__name__}: {e}"[:200])

    bg_tasks = request.app.state.agent_bg_tasks
    task = asyncio.create_task(_run())
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return {"job_id": job_id}


@router.get("/v1/steam/fetch-manifests/{job_id}")
async def get_fetch_manifests(job_id: str, request: Request) -> dict[str, Any]:
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


class PruneSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exclude_app_ids: list[int] = Field(default_factory=list)
    restore_app_ids: list[int] = Field(default_factory=list)


@router.post("/v1/steam/prune-selection")
async def prune_selection(body: PruneSelectionRequest, request: Request) -> dict[str, Any]:
    """Reconcile SteamPrefill's selectedAppsToPrefill.json (Piece 1): remove
    ``exclude_app_ids`` (classifier non-games) and ensure ``restore_app_ids``
    (operator 'allow') are present, so the host SteamPrefill cron stops caching
    the non-games. The original curated list is preserved once in a `.bak`
    sidecar; a no-op change writes nothing. Idempotent."""
    s = request.app.state.settings
    path = Path(s.steam_prefill_config_dir) / "selectedAppsToPrefill.json"
    if not path.exists():
        return {"removed": 0, "restored": 0, "remaining": 0, "note": "no selection file"}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"removed": 0, "restored": 0, "remaining": 0, "note": "unreadable"}
    current = data if isinstance(data, list) else []
    new, removed, restored = reconcile_selection(
        current, exclude_ids=body.exclude_app_ids, restore_ids=body.restore_app_ids
    )
    if removed or restored:
        try:
            bak = path.parent / "selectedAppsToPrefill.json.bak"
            if not bak.exists():  # preserve the ORIGINAL curated list, once
                bak.write_text(path.read_text())
            path.write_text(json.dumps(new))
        except OSError as e:
            _log.error("agent.prune_selection.write_failed", reason=str(e)[:200])
            raise HTTPException(status_code=500, detail="selection write failed") from e
    _log.info("agent.prune_selection.done", removed=removed, restored=restored, remaining=len(new))
    return {"removed": removed, "restored": restored, "remaining": len(new)}


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


def _prefilled_gids(request: Request, app_id: int) -> set[str]:
    """The gids SteamPrefill actually downloaded for this app (its own record),
    used to pin manifest selection to the CURRENT prefilled version rather than
    the newest manifest on disk — a stale newer build is the false-Partial root
    cause. Best-effort: tolerant of a missing/unreadable driver or file (returns
    an empty set → newest-by-mtime fallback)."""
    try:
        state = request.app.state.prefill_driver.downloaded_state()
        return {str(g) for g in state.get(app_id, [])}
    except Exception:
        return set()


def _steam_chunk_paths(
    settings: Any, app_id: int, prefilled_gids: set[str]
) -> tuple[dict[int, list[Path]], list[str], int, bool]:
    """Locate the app's manifest .bin/.shas files, parse chunk SHAs, and derive
    the nginx cache path for each unique (depot, sha). Shared by
    ``/v1/steam/validate`` and ``/v1/steam/purge`` (DRY — the single source of the
    manifest→cache-path enumeration). Returns
    ``(depot_paths, versions, parsed_ok, bins_found)``:

      * ``bins_found`` False → no manifest in cache for this app.
      * ``parsed_ok == 0`` with ``bins_found`` True → manifests present but none
        parseable.

    A corrupt/foreign manifest (non-numeric depot field, unreadable file) is
    skipped, never fatal (COR-1). ``.shas`` is the fetcher's sidecar (one SHA per
    line); ``.bin`` is SteamPrefill's protobuf — same
    ``{app}_{app}_{depot}_{gid}`` filename layout.
    """
    cache_root = Path(settings.lancache_nginx_cache_path)
    roots = [Path(settings.steam_manifest_cache_dir), Path(settings.steam_manifest_archive_dir)]
    bins = locate_manifest_bins(app_id, cache_roots=roots, prefilled_gids=prefilled_gids or None)
    if not bins:
        return {}, [], 0, False

    slice_range = slice_range_zero(settings.cache_slice_size_bytes)
    identifier = settings.steam_cache_identifier
    levels = settings.cache_levels

    seen: set[tuple[int, str]] = set()
    depot_paths: dict[int, list[Path]] = {}
    versions: list[str] = []
    parsed_ok = 0
    for binpath in bins:
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
    return depot_paths, versions, parsed_ok, True


@router.post("/v1/steam/validate")
async def steam_validate(body: SteamValidateRequest, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    prefilled_gids = _prefilled_gids(request, body.app_id)
    depot_paths, versions, parsed_ok, bins_found = _steam_chunk_paths(
        settings, body.app_id, prefilled_gids
    )
    if not bins_found:
        return {
            "chunks_total": 0,
            "chunks_cached": 0,
            "chunks_missing": 0,
            "outcome": "error",
            "versions": "",
            "error": "no_manifest_in_cache",
        }
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
    # but are empty (size 0) has present > 0 and is KEPT, so a genuine gap stays
    # visible instead of being silently dropped as "never prefilled". (Transient
    # mode-000 files now count as cached — see disk_stat._stat_batch — since they
    # self-heal in ms; a depot fully evicted to 0 files on disk is indistinguishable
    # from never-prefilled and is excluded — accepted: whole-depot eviction-to-zero
    # is rare under per-file LRU.)
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


class SteamPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: int = Field(..., ge=0)


@router.post("/v1/steam/purge")
async def steam_purge(body: SteamPurgeRequest, request: Request) -> dict[str, int]:
    """Delete a Steam game's cached chunk files (F18). Enumerates the SAME chunk
    paths as ``/v1/steam/validate`` (via ``_steam_chunk_paths``, pinned to the
    prefilled gid), applies the cache-root path-safety guard, then unlinks each.

    Idempotent: a never-cached app (no manifest in cache, or manifests present but
    no files on disk) returns ``{deleted: 0}`` — never an error. The control plane
    sets ``status='validation_failed'`` afterward so F5/F6 re-prefills a fresh copy
    (ADR-0015 — purge is reversible)."""
    settings = request.app.state.settings
    prefilled_gids = _prefilled_gids(request, body.app_id)
    depot_paths, _versions, _parsed_ok, _bins_found = _steam_chunk_paths(
        settings, body.app_id, prefilled_gids
    )
    # Purge the whole game: every enumerated chunk across all depots (no depot-
    # scoping — purge_chunks no-ops on paths that aren't present).
    paths = [p for dpaths in depot_paths.values() for p in dpaths]
    safe = under_cache_root(Path(settings.lancache_nginx_cache_path), paths)
    deleted, failed, freed = await purge_chunks(safe)
    _log.info(
        "agent.steam_purge",
        app_id=body.app_id,
        deleted=deleted,
        failed=failed,
        bytes_freed=freed,
    )
    return {"deleted": deleted, "failed": failed, "bytes_freed": freed}
