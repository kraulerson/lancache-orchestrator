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

import json
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

# Title-preserving upsert for the SteamPrefill-sourced enumeration: list_owned
# gives app_ids without names, so a NEW app gets the app_id as its placeholder
# title (set on INSERT), while an EXISTING app keeps its title — only `owned`
# is updated on conflict.
_PREFILL_UPSERT_SQL = (
    "INSERT INTO games (platform, app_id, title) VALUES ('steam', ?, ?) "
    "ON CONFLICT(platform, app_id) DO UPDATE SET owned = 1"
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
    agent's downloaded-state ({app_id: [gids]}) rather than the orchestrator's
    driver (the control plane has no /SteamPrefill mount). The keys are the
    prefilled app_ids; the title-preserving upsert keeps any existing name and
    uses the app_id as the placeholder title for new apps."""
    if deps.agent_client is None:
        raise RuntimeError("agent_client is required when steam_enumerate_via_prefill")
    job_id = job.get("id")
    state = await deps.agent_client.downloaded_state()
    app_ids = list(state)
    _log.info("library_sync.prefill.enumerate.returned", job_id=job_id, app_count=len(app_ids))
    for app_id in app_ids:
        await deps.pool.execute_write(_PREFILL_UPSERT_SQL, (str(app_id), str(app_id)))
    _log.info("library_sync.prefill.upserted", job_id=job_id, upserted=len(app_ids))


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
