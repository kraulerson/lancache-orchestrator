"""Steam library sync handler (BL11).

Called by the jobs worker when a `library_sync` job is claimed. Calls
`library.enumerate` on the steam worker subprocess and upserts the
operator's owned apps into the `games` table.

Idempotent: `INSERT ... ON CONFLICT(platform, app_id) DO UPDATE` updates
title/owned/metadata only — `status`, `cached_version`, and other
lifecycle columns are preserved (plan P11).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

_UPSERT_SQL = (
    "INSERT INTO games (platform, app_id, title, owned, metadata) "
    "VALUES (?, ?, ?, 1, ?) "
    "ON CONFLICT(platform, app_id) DO UPDATE SET "
    "  title = excluded.title, "
    "  owned = 1, "
    "  metadata = excluded.metadata"
)


async def library_sync_handler(job: dict[str, Any], deps: Deps) -> None:
    """Library sync handler.

    Raises:
        ValueError — `job.platform` is not 'steam'.
        RuntimeError — `deps.steam_client` is None.
        IPCTimeoutError / WorkerDiedError / WorkerDisabledError — propagate from
            SteamWorkerClient. Worker loop translates to job state=failed.
        SteamWorkerError — propagate. When kind == 'NotAuthenticated', the
            handler ALSO updates `platforms.auth_status='expired'` so the
            operator-facing /platforms surface matches reality before the
            re-raise marks the job failed (F-UAT6-3).
    """
    from orchestrator.platform.steam.client import SteamWorkerError

    platform = job.get("platform")
    if platform != "steam":
        raise ValueError(f"library_sync only supports steam (got {platform!r})")
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
        await deps.pool.execute_write(_UPSERT_SQL, ("steam", str(app_id_raw), title, metadata))
        upserted += 1

    _log.info(
        "library_sync.upserted",
        job_id=job_id,
        upserted=upserted,
        skipped=skipped,
    )
