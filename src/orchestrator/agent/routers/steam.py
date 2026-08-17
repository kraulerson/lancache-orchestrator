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


def _prefill_gate(request: Request) -> dict[str, Any]:
    """Single-flight state for SteamPrefill, held on app.state.

    SteamPrefill is not built for concurrent invocations sharing one auth/cache
    (re-arch ① §6), so the agent serialises the runs it starts itself.

    SCOPE — this gate covers AGENT-INITIATED runs only. It is an in-process dict,
    not an OS-level lock, and the driver does not take the host cron's
    `steamprefill.lock`. A cron tick at 04:00 and an agent-initiated run at 03:59
    can therefore still overlap; that window closes only when the host cron is
    retired (Phase 4). An earlier version of this docstring claimed the cron's
    flock protected us — it does not, since the two writers share no lock file.

    Carries the request `signature` alongside the job id so an in-flight run can
    only absorb a request that is genuinely the same work — see `start_prefill`.
    """
    state = request.app.state
    if not hasattr(state, "steam_prefill_gate"):
        state.steam_prefill_gate = {"job_id": None, "signature": None}
    gate: dict[str, Any] = state.steam_prefill_gate
    gate.setdefault("signature", None)  # tolerate a gate seeded without one
    return gate


def _in_flight(request: Request) -> tuple[str | None, Any]:
    """Return ``(job_id, signature)`` for a genuinely-running prefill, else
    ``(None, None)``, clearing the gate.

    FAILS OPEN. A job id the store no longer knows about (``snap is None``) is
    treated as stale and cleared, not as still-running. The previous version
    inverted this: it returned the unknown id forever, so every subsequent
    request 202'd with a dead job_id, the control plane polled a 404 and raised,
    and Steam prefill was wedged until the agent process restarted. A missing
    snapshot IS the stale case the function exists to clear.
    """
    gate = _prefill_gate(request)
    job_id: str | None = gate["job_id"]
    if job_id is None:
        return None, None
    store = request.app.state.agent_jobs
    snap = store.get(job_id)
    if snap is None or snap["state"] in ("done", "failed"):
        gate["job_id"] = None
        gate["signature"] = None
        return None, None
    return job_id, gate["signature"]


def _prefill_signature(app_ids: list[int], force: bool) -> tuple[tuple[int, ...], bool]:
    """Identity of a prefill request: which apps, and whether it forces.

    Order-insensitive on app_ids — [730, 440] and [440, 730] are the same work.
    """
    return (tuple(sorted(app_ids)), force)


@router.post("/v1/steam/prefill", status_code=status.HTTP_202_ACCEPTED)
async def start_prefill(body: SteamPrefillRequest, request: Request) -> dict[str, str]:
    _validate_app_ids(body.app_ids)
    signature = _prefill_signature(body.app_ids, body.force)
    existing, existing_signature = _in_flight(request)
    if existing is not None:
        if existing_signature == signature:
            # Genuinely the same work — absorbing it is correct.
            _log.info("steam_prefill.dedup_hit", job_id=existing)
            return {"job_id": existing}
        # DIFFERENT work. Returning the in-flight id here would make the control
        # plane record THIS request's games as prefilled when a different app was
        # downloaded (and would silently downgrade a force refill to non-force).
        # Refuse loudly instead: AgentClient turns non-2xx into AgentError, so the
        # job fails honestly and can be retried once the current run finishes.
        _log.warning(
            "steam_prefill.conflict",
            in_flight_job_id=existing,
            requested_app_ids=body.app_ids,
            requested_force=body.force,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"a different SteamPrefill run is already in flight as job {existing}; "
                "retry once it completes"
            ),
        )

    driver = request.app.state.prefill_driver
    settings = request.app.state.settings
    store = request.app.state.agent_jobs
    gate = _prefill_gate(request)
    job_id = store.create()
    gate["job_id"] = job_id
    gate["signature"] = signature

    async def _run() -> None:
        try:
            result = await driver.prefill_apps(body.app_ids, force=body.force)
            if result.ok:
                _capture_prefill_manifests(job_id, settings)
            store.set_done(job_id, {"ok": result.ok, "raw": result.raw})
        except Exception as e:  # record, never crash the loop
            store.set_failed(job_id, f"{type(e).__name__}: {e}"[:200])
        finally:
            if gate["job_id"] == job_id:
                gate["job_id"] = None
                gate["signature"] = None

    # Hold a strong reference so the fire-and-forget task is not GC'd mid-flight
    # (mirrors the /v1/pull background-task set + discard-on-done pattern).
    bg_tasks = request.app.state.agent_bg_tasks
    task = asyncio.create_task(_run())
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return {"job_id": job_id}


def _capture_prefill_manifests(job_id: str, settings: Any) -> None:
    """After a successful SteamPrefill run, capture the manifest(s) it just
    wrote to its HOME cache into the durable archive.

    Shared by every prefill mode: the periodic archive-sync only reads the
    host cache, so without this an agent-driven run's manifest is never
    archived and validate falls back to a stale older manifest — the
    false-Partial root cause. settle_seconds=0.0 because the run just
    finished. A capture failure must never fail the job.
    """
    # A successful prefill always writes its manifest(s) to the HOME cache, so
    # a MISSING live cache dir means SteamPrefill's HOME and
    # steam_prefill_live_cache_dir have drifted apart — the capture would
    # silently no-op and false-Partial badges would silently return. Make that
    # loud (UAT-13 F2b). (The driver pins HOME from this same setting, so this
    # should never fire — it's the canary.)
    live_v1 = Path(settings.steam_prefill_live_cache_dir) / "v1"
    if not live_v1.is_dir():
        _log.warning(
            "steam_prefill.live_cache_missing",
            job_id=job_id,
            live_cache=str(live_v1),
            hint="HOME/.cache path mismatch; manifests NOT captured — check agent HOME",
        )
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

    # A prefill owns selectedAppsToPrefill.json for the duration of its run:
    # `prefill_apps` overwrites it with a temporary one-app list and restores it
    # afterwards. The fetcher's `_enumerate_app_ids` reads that same file, so an
    # overlap enumerates ~1 app instead of the whole library — silently, with
    # normal-looking fetched/skipped counts, quietly reopening the .shas coverage
    # gap #213 exists to close.
    prefill_job, _ = _in_flight(request)
    if prefill_job is not None:
        _log.warning("steam_fetch_manifests.conflict", in_flight_prefill_job_id=prefill_job)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"a SteamPrefill run is in flight as job {prefill_job} and owns the app "
                "selection; retry once it completes"
            ),
        )

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
    line), always ``{app}_{app}_{depot}_{gid}.shas``. ``.bin`` is SteamPrefill's
    protobuf; only a game's PRIMARY depot repeats the app id in that shape — a
    secondary depot is ``{app}_{depotGroupId}_{depot}_{gid}.bin`` with a
    differing group id. See ``manifest_locator.py`` module docstring.
    """
    cache_root = Path(settings.lancache_nginx_cache_path)
    roots = [Path(settings.steam_manifest_cache_dir), Path(settings.steam_manifest_archive_dir)]
    bins = locate_manifest_bins(app_id, cache_roots=roots, prefilled_gids=prefilled_gids or None)
    if not bins:
        return {}, [], 0, False

    slice_range = slice_range_zero(settings.cache_slice_size_bytes)
    identifier = settings.steam_cache_identifier
    levels = settings.cache_levels

    shared_redist = settings.steam_shared_redist_depots
    seen: set[tuple[int, str]] = set()
    depot_paths: dict[int, list[Path]] = {}
    versions: list[str] = []
    skipped_redist: set[int] = set()
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
        if depot_id in shared_redist:
            # Shared Steamworks Common Redistributables (app 228980) — runtime
            # content shared across many games, only ever partially cached. Skip it
            # so it doesn't drag a fully-cached game to 'partial', and so purge
            # never deletes chunks other games depend on. The manifest still parsed
            # (parsed_ok counts it) so an all-redist enumeration isn't a false error.
            skipped_redist.add(depot_id)
            continue
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
    if skipped_redist:
        _log.info(
            "steam_validate.shared_redist_skipped",
            app_id=app_id,
            depots=sorted(skipped_redist),
        )
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
