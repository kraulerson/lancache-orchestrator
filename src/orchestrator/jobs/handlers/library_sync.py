"""Steam library sync handler (BL11).

Called by the jobs worker when a `library_sync` job is claimed. Calls
`library.enumerate` on the steam worker subprocess and upserts the
operator's owned apps into the `games` table.

Idempotent: `INSERT ... ON CONFLICT(platform, app_id) DO UPDATE` updates
title/owned/metadata/current_version only — `status`, `cached_version`, and
other lifecycle columns are preserved (plan P11). `current_version` carries the
latest upstream version token (F8) so the scheduled-prefill diff can detect
patches without clobbering what was last cached.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.platform.steam.store import fetch_app_info

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

# Name-bearing upsert for the SteamPrefill-sourced enumeration (re-arch ③b):
# the Steam store lookup gives us the real title, so a NEW app is inserted with
# it and an EXISTING app has its title refreshed to the store name. Lifecycle
# columns (status, cached_version, …) are preserved.
_NAMED_UPSERT_SQL = (
    "INSERT INTO games (platform, app_id, title) VALUES ('steam', ?, ?) "
    "ON CONFLICT(platform, app_id) DO UPDATE SET title = excluded.title, owned = 1"
)

# Idempotent upsert into the store-lookup cache.
_APP_INFO_UPSERT_SQL = (
    "INSERT INTO steam_app_info (app_id, app_type, name) VALUES (?, ?, ?) "
    "ON CONFLICT(app_id) DO UPDATE SET "
    "app_type = excluded.app_type, name = excluded.name, fetched_at = datetime('now')"
)

_UPSERT_SQL = (
    "INSERT INTO games (platform, app_id, title, owned, metadata, current_version) "
    "VALUES (?, ?, ?, 1, ?, ?) "
    "ON CONFLICT(platform, app_id) DO UPDATE SET "
    "  title = excluded.title, "
    "  owned = 1, "
    "  metadata = excluded.metadata, "
    # Keep a known-good version if this enumeration didn't carry one (a depot
    # with no buildid, or a transient gap) — never erase version tracking.
    "  current_version = COALESCE(excluded.current_version, games.current_version)"
)


async def library_sync_handler(job: dict[str, Any], deps: Deps) -> None:
    """Library sync handler — dispatches on ``job.platform``."""
    platform = job.get("platform")
    if platform == "steam":
        return await _steam_library_sync(job, deps)
    if platform == "epic":
        return await _epic_library_sync(job, deps)
    raise ValueError(f"library_sync: unsupported platform {platform!r}")


async def _steam_library_sync_via_prefill(job: dict[str, Any], deps: Deps) -> None:
    """Enumerate the Steam library from SteamPrefill's prefilled apps (re-arch
    ③b). SteamPrefill lives on the lancache host (agent side), so this reads the
    agent's prefilled-apps — the distinct app_ids from the manifest .bin cache
    filenames (real game app_ids), NOT successfullyDownloadedDepots.json (whose
    keys are depot_ids with no store page).

    Some prefilled app_ids may still be DLC; to upsert only actual games (with
    their real names) we look each uncached app up via the public Steam store
    appdetails API (no auth) for its {type, name}, cache the result in
    steam_app_info, and upsert only type=='game'. The store API is rate-limited
    (~200/5min) so each run is bound by steam_store_fetch_budget; the rest fill
    on later scheduled syncs."""
    if deps.agent_client is None:
        raise RuntimeError("agent_client is required when steam_enumerate_via_prefill")
    settings = get_settings()
    job_id = job.get("id")
    app_ids = [str(a) for a in await deps.agent_client.prefilled_apps()]
    _log.info("library_sync.prefill.enumerate.returned", job_id=job_id, app_count=len(app_ids))

    cache: dict[str, dict[str, str]] = {
        r["app_id"]: {"type": r["app_type"], "name": r["name"]}
        for r in await deps.pool.read_all("SELECT app_id, app_type, name FROM steam_app_info")
    }
    budget = settings.steam_store_fetch_budget
    delay = settings.steam_store_fetch_delay_sec
    fetched = 0
    upserted = 0
    for app_id in app_ids:
        info = cache.get(app_id)
        if info is None and fetched < budget:
            detail = await fetch_app_info(int(app_id))
            fetched += 1
            if delay > 0:
                await asyncio.sleep(delay)
            if detail is not None:
                await deps.pool.execute_write(
                    _APP_INFO_UPSERT_SQL, (app_id, detail["type"], detail["name"])
                )
                info = detail
        if info is not None and info["type"] == "game":
            await deps.pool.execute_write(_NAMED_UPSERT_SQL, (app_id, info["name"]))
            upserted += 1
    _log.info(
        "library_sync.prefill.upserted",
        job_id=job_id,
        app_count=len(app_ids),
        fetched=fetched,
        upserted=upserted,
    )


async def _epic_library_sync(job: dict[str, Any], deps: Deps) -> None:
    """Enumerate the owned Epic library and upsert into ``games`` (F6)."""
    from orchestrator.platform.epic.client import EpicNotAuthenticatedError

    if deps.epic_client is None:
        raise RuntimeError("epic_client is required for epic library_sync")
    job_id = job.get("id")
    _log.info("library_sync.epic.enumerate.started", job_id=job_id)
    try:
        items = await deps.epic_client.library_enumerate()
    except EpicNotAuthenticatedError as e:
        # Surface the expired session on the platforms row, mirroring Steam.
        try:
            await deps.pool.execute_write(
                "UPDATE platforms SET auth_status='expired', last_error=? WHERE name='epic'",
                (f"NotAuthenticated: {e}"[:200],),
            )
        except Exception as upd_e:  # best-effort mark; we still re-raise below
            _log.error("library_sync.epic.session_mark_failed", reason=str(upd_e)[:200])
        raise
    _log.info("library_sync.epic.enumerate.returned", job_id=job_id, app_count=len(items))

    upserted = 0
    for item in items:
        metadata = json.dumps(
            {"namespace": item.namespace, "catalog_item_id": item.catalog_item_id},
            separators=(",", ":"),
        )
        await deps.pool.execute_write(
            _UPSERT_SQL, ("epic", item.app_name, item.title, metadata, item.build_version)
        )
        upserted += 1
    _log.info("library_sync.epic.upserted", job_id=job_id, upserted=upserted)


async def _steam_library_sync(job: dict[str, Any], deps: Deps) -> None:
    """Steam library sync.

    Raises:
        RuntimeError — `deps.steam_client` is None.
        IPCTimeoutError / WorkerDiedError / WorkerDisabledError — propagate from
            SteamWorkerClient. Worker loop translates to job state=failed.
        SteamWorkerError — propagate. When kind == 'NotAuthenticated', the
            handler ALSO updates `platforms.auth_status='expired'` so the
            operator-facing /platforms surface matches reality before the
            re-raise marks the job failed (F-UAT6-3).
    """
    if get_settings().steam_enumerate_via_prefill:
        return await _steam_library_sync_via_prefill(job, deps)

    from orchestrator.platform.steam.client import SteamWorkerError

    if deps.steam_client is None:
        raise RuntimeError("steam_client is required for library_sync handler")

    job_id = job.get("id")
    _log.info("library_sync.enumerate.started", job_id=job_id)
    try:
        result = await deps.steam_client.library_enumerate()
    except SteamWorkerError as e:
        if e.kind == "NotAuthenticated":
            # F-UAT6-3: surface the expired-session state on the
            # platforms row so /api/v1/platforms and /api/v1/.../auth/status
            # don't disagree. Best-effort — if this UPDATE fails we still
            # re-raise the original error so the job is marked failed.
            try:
                await deps.pool.execute_write(
                    "UPDATE platforms SET auth_status='expired', last_error=? WHERE name='steam'",
                    (f"NotAuthenticated: {e.message}"[:200],),
                )
                _log.warning(
                    "library_sync.session_expired_marked",
                    job_id=job_id,
                )
            except Exception as upd_e:
                _log.error(
                    "library_sync.session_expired_mark_failed",
                    job_id=job_id,
                    reason=str(upd_e)[:200],
                )
        raise
    apps = result.get("apps") or []
    _log.info("library_sync.enumerate.returned", job_id=job_id, app_count=len(apps))

    upserted = 0
    skipped = 0
    for app in apps:
        app_id_raw = app.get("app_id")
        title = app.get("name")
        depots = app.get("depots") or []
        if app_id_raw is None or not isinstance(title, str) or not title:
            skipped += 1
            _log.warning(
                "library_sync.skipped_app",
                job_id=job_id,
                reason="missing app_id or name",
                raw=str(app)[:200],
            )
            continue
        metadata = json.dumps(
            {"depots": list(depots), "steam_packages": []},
            separators=(",", ":"),
        )
        version = app.get("version")
        await deps.pool.execute_write(
            _UPSERT_SQL, ("steam", str(app_id_raw), title, metadata, version)
        )
        upserted += 1

    _log.info(
        "library_sync.upserted",
        job_id=job_id,
        upserted=upserted,
        skipped=skipped,
    )
