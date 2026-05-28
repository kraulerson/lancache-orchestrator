"""BL12 — Steam manifest fetcher handler.

Called by the jobs worker when a `manifest_fetch` job is claimed.
Asks the steam worker subprocess to enumerate the operator's owned
depot manifests for a single game, then upserts the `manifests`
table.

Per ADR-0013 D14: the orchestrator NEVER deserializes the manifest
BLOB. The worker compresses + base64-encodes the raw protobuf bytes;
this handler decodes the base64 and stores the bytes as an opaque
BLOB. The F7 validator (when it ships) will deserialize the BLOB
inside the worker venv.

Per spike-A3 (`spikes/spike_a3_steam_manifest.md`):
- IPC contract: worker returns `{manifests: [{depot_id, manifest_gid,
  name, total_bytes, chunk_count, raw_b64}, ...]}` — one entry per
  depot for the requested app_id.
- Schema: UPSERT ON CONFLICT(game_id, version) — re-fetch is
  idempotent; new manifest_gid creates a new row, old version stays
  in the table as historical record.
- `games.size_bytes` set to the SUM of all manifest total_bytes
  (full install size across depots).
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

_UPSERT_SQL = (
    "INSERT INTO manifests "
    "(game_id, version, fetched_at, chunk_count, total_bytes, raw) "
    "VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?) "
    "ON CONFLICT(game_id, version) DO UPDATE SET "
    "  fetched_at = CURRENT_TIMESTAMP, "
    "  chunk_count = excluded.chunk_count, "
    "  total_bytes = excluded.total_bytes, "
    "  raw = excluded.raw"
)


async def manifest_fetch_handler(job: dict[str, Any], deps: Deps) -> None:
    """Manifest fetcher handler (BL12).

    Raises:
        ValueError — non-steam platform, game_id not found, or a
            single manifest exceeds `Settings.manifest_size_cap_bytes`
            (anomaly guard).
        RuntimeError — `deps.steam_client` is None.
        IPCTimeoutError / WorkerDiedError / WorkerDisabledError — propagate
            from SteamWorkerClient. Worker loop translates to
            job state=failed.
        SteamWorkerError — propagate. When kind == 'NotAuthenticated',
            the handler ALSO updates `platforms.auth_status='expired'`
            before re-raising (mirrors library_sync's F-UAT6-3 fix).
    """
    from orchestrator.platform.steam.client import SteamWorkerError

    platform = job.get("platform")
    if platform != "steam":
        raise ValueError(f"manifest_fetch only supports steam (got {platform!r})")
    if deps.steam_client is None:
        raise RuntimeError("steam_client is required for manifest_fetch handler")

    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("manifest_fetch job has no game_id")
    job_id = job.get("id")

    # Look up app_id from games table.
    game_row = await deps.pool.read_one("SELECT app_id, platform FROM games WHERE id=?", (game_id,))
    if game_row is None:
        raise ValueError(f"game {game_id} not found in games table")
    if game_row["platform"] != "steam":
        raise ValueError(f"game {game_id} platform is {game_row['platform']!r}, not steam")
    try:
        app_id_int = int(game_row["app_id"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"game {game_id} app_id {game_row['app_id']!r} is not numeric") from e

    _log.info("manifest_fetch.started", job_id=job_id, game_id=game_id, app_id=app_id_int)

    try:
        result = await deps.steam_client.manifest_fetch(app_id_int)
    except SteamWorkerError as e:
        if e.kind == "NotAuthenticated":
            try:
                await deps.pool.execute_write(
                    "UPDATE platforms SET auth_status='expired', last_error=? WHERE name='steam'",
                    (f"NotAuthenticated: {e.message}"[:200],),
                )
                _log.warning("manifest_fetch.session_expired_marked", job_id=job_id)
            except Exception as upd_e:
                _log.error(
                    "manifest_fetch.session_expired_mark_failed",
                    job_id=job_id,
                    reason=str(upd_e)[:200],
                )
        raise

    manifests = result.get("manifests") or []
    _log.info(
        "manifest_fetch.returned",
        job_id=job_id,
        game_id=game_id,
        manifest_count=len(manifests),
    )

    if not manifests:
        # Empty result is success; no DB writes, no size_bytes update.
        return

    settings = get_settings()
    cap = settings.manifest_size_cap_bytes

    upserted = 0
    total_size = 0
    for m in manifests:
        depot_id = m.get("depot_id")
        gid = m.get("manifest_gid")
        total_bytes = m.get("total_bytes")
        chunk_count = m.get("chunk_count")
        raw_b64 = m.get("raw_b64")

        if (
            depot_id is None
            or gid is None
            or total_bytes is None
            or chunk_count is None
            or not raw_b64
        ):
            _log.warning(
                "manifest_fetch.skipped_entry",
                job_id=job_id,
                reason="missing required field",
                raw=str(m)[:200],
            )
            continue

        try:
            raw_bytes = base64.b64decode(raw_b64)
        except (ValueError, TypeError) as e:
            _log.warning(
                "manifest_fetch.skipped_entry",
                job_id=job_id,
                reason=f"base64 decode failed: {type(e).__name__}",
                depot_id=depot_id,
            )
            continue

        if len(raw_bytes) > cap:
            raise ValueError(
                f"manifest depot_id={depot_id} gid={gid} exceeds size cap "
                f"({len(raw_bytes)} > {cap} bytes)"
            )

        await deps.pool.execute_write(
            _UPSERT_SQL,
            (game_id, str(gid), int(chunk_count), int(total_bytes), raw_bytes),
        )
        upserted += 1
        total_size += int(total_bytes)

    if upserted == 0:
        # All entries had missing fields — log and exit without touching games.
        _log.warning(
            "manifest_fetch.no_valid_entries",
            job_id=job_id,
            received=len(manifests),
        )
        return

    # Update games.size_bytes = sum of manifest total_bytes (spec §6.3).
    await deps.pool.execute_write(
        "UPDATE games SET size_bytes=? WHERE id=?",
        (total_size, game_id),
    )

    _log.info(
        "manifest_fetch.upserted",
        job_id=job_id,
        game_id=game_id,
        upserted=upserted,
        skipped=len(manifests) - upserted,
        total_size_bytes=total_size,
    )
