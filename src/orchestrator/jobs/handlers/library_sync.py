"""Steam library sync handler (BL11 / re-arch ③b).

Called by the jobs worker when a `library_sync` job is claimed. Enumerates the
prefilled Steam library via the data-plane agent (SteamPrefill manifest cache)
and upserts the operator's owned apps into the `games` table.

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

# Idempotent upsert into the store-lookup cache (incl. MP-only category flags, #366).
_APP_INFO_UPSERT_SQL = (
    "INSERT INTO steam_app_info (app_id, app_type, name, has_single_player, has_multiplayer) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(app_id) DO UPDATE SET "
    "app_type = excluded.app_type, name = excluded.name, "
    "has_single_player = excluded.has_single_player, "
    "has_multiplayer = excluded.has_multiplayer, fetched_at = datetime('now')"
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


async def _steam_library_sync(job: dict[str, Any], deps: Deps) -> None:
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
        raise RuntimeError("agent_client is required for steam library_sync")
    settings = get_settings()
    job_id = job.get("id")
    app_ids = [str(a) for a in await deps.agent_client.prefilled_apps()]
    _log.info("library_sync.prefill.enumerate.returned", job_id=job_id, app_count=len(app_ids))

    cache: dict[str, dict[str, Any]] = {
        r["app_id"]: {
            "type": r["app_type"],
            "name": r["name"],
            "has_single_player": r["has_single_player"],
            "has_multiplayer": r["has_multiplayer"],
        }
        for r in await deps.pool.read_all(
            "SELECT app_id, app_type, name, has_single_player, has_multiplayer FROM steam_app_info"
        )
    }
    budget = settings.steam_store_fetch_budget
    delay = settings.steam_store_fetch_delay_sec
    fetched = 0
    upserted = 0
    for app_id in app_ids:
        info = cache.get(app_id)
        # Fetch a NEW app, or backfill an old row whose category flags predate
        # MP-only tracking (has_single_player IS NULL) — budget-bound (#366).
        needs_fetch = info is None or info.get("has_single_player") is None
        if needs_fetch and fetched < budget:
            detail = await fetch_app_info(int(app_id))
            fetched += 1
            if delay > 0:
                await asyncio.sleep(delay)
            if detail is not None:
                await deps.pool.execute_write(
                    _APP_INFO_UPSERT_SQL,
                    (
                        app_id,
                        detail["type"],
                        detail["name"],
                        detail["has_single_player"],
                        detail["has_multiplayer"],
                    ),
                )
                info = dict(detail)
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
