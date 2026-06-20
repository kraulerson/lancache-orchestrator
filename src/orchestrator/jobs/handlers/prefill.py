"""F5 — prefill job handler.

Steam: delegates to the host-installed SteamPrefill binary via
``SteamPrefillDriver`` (modern persistent auth), which downloads the app's
content through the lancache so it gets cached. Epic: fetches a fresh signed
manifest and downloads chunks through our own downloader. On success either
path enqueues a ``validate`` job (ID5), which sets the game's final status.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.platform.epic.manifest import chunk_path as epic_chunk_path
from orchestrator.platform.epic.models import EpicLibraryItem
from orchestrator.prefill.epic_downloader import prefill_chunks as epic_prefill_chunks
from orchestrator.prefill.epic_downloader import verify_cached as epic_verify_cached

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps
    from orchestrator.platform.epic.client import EpicClient
    from orchestrator.platform.steam.prefill_driver import SteamPrefillDriver

_log = structlog.get_logger(__name__)


def _summarize_failures(failures: list[tuple[str, str]], *, top: int = 5) -> dict[str, int]:
    """Tally prefill chunk-failure reasons (e.g. ``{'http 403': 2418,
    'ConnectError': 12}``), keeping the ``top`` most common (#169). Lets a failed
    prefill be diagnosed from the log / the game's ``last_error`` without code
    spelunking — e.g. an `http 403` (CDN auth token needed) vs `http 404`
    (chunk not found) vs `ConnectError` (lancache down)."""
    return dict(Counter(reason for _uri, reason in failures).most_common(top))


def _failure_suffix(failure_reasons: dict[str, int]) -> str:
    """`` (http 403: 2418, ConnectError: 12)`` for last_error, or `""` if none."""
    if not failure_reasons:
        return ""
    return " (" + ", ".join(f"{r}: {n}" for r, n in failure_reasons.items()) + ")"


# Epic manifest upsert (depot_id is NULL — Epic has no depots). Keyed on
# (game_id, version); a re-fetch updates the existing row.
_EPIC_MANIFEST_UPSERT = (
    "INSERT INTO manifests (game_id, depot_id, version, fetched_at, chunk_count, total_bytes, raw) "
    "VALUES (?, NULL, ?, CURRENT_TIMESTAMP, ?, ?, ?) "
    "ON CONFLICT(game_id, version) DO UPDATE SET "
    "  fetched_at = CURRENT_TIMESTAMP, "
    "  chunk_count = excluded.chunk_count, "
    "  total_bytes = excluded.total_bytes, "
    "  raw = excluded.raw"
)


async def prefill_handler(job: dict[str, Any], deps: Deps) -> None:
    """Prefill one game's chunks through the lancache — dispatches on platform."""
    platform = job.get("platform")
    if platform == "steam":
        return await _steam_prefill(job, deps)
    if platform == "epic":
        return await _epic_prefill(job, deps)
    raise ValueError(f"prefill: unsupported platform {platform!r}")


async def _epic_prefill(job: dict[str, Any], deps: Deps) -> None:
    """Prefill one Epic game (F6): set downloading → fetch a FRESH manifest
    (Epic signed URLs expire) → store it → download chunks through the lancache →
    sample-verify the cache HIT → mark up_to_date. F7-Epic disk-stat validation is
    a deferred follow-up, so the inline HIT verification is the validation here."""
    if deps.epic_client is None:
        raise RuntimeError("epic_client is required for epic prefill handler")
    epic_client = deps.epic_client
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("prefill job has no game_id")
    game = await deps.pool.read_one(
        "SELECT id, app_id, title, platform, metadata FROM games WHERE id=?", (game_id,)
    )
    if game is None:
        raise ValueError(f"game {game_id} not found in games table")
    if game["platform"] != "epic":
        raise ValueError(f"game {game_id} platform is {game['platform']!r}, not epic")

    job_id = job.get("id")
    await deps.pool.execute_write("UPDATE games SET status='downloading' WHERE id=?", (game_id,))
    _log.info("prefill.epic.started", job_id=job_id, game_id=game_id)
    try:
        await _epic_prefill_inner(job_id, game_id, game, deps, epic_client)
    except Exception as e:
        # Never leave the game stuck in 'downloading'. The chunk-failure path
        # already set 'failed' (the guard then no-ops); any other failure
        # (auth/manifest/network) is marked here before the re-raise.
        with contextlib.suppress(Exception):
            await deps.pool.execute_write(
                "UPDATE games SET status='failed', last_error=? "
                "WHERE id=? AND status='downloading'",
                (f"prefill: {type(e).__name__}: {e}"[:200], game_id),
            )
        raise


async def _epic_prefill_inner(
    job_id: Any,
    game_id: int,
    game: dict[str, Any],
    deps: Deps,
    epic_client: EpicClient,
) -> None:
    try:
        meta = json.loads(game["metadata"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    item = EpicLibraryItem(
        app_name=str(game["app_id"]),
        namespace=str(meta.get("namespace", "")),
        catalog_item_id=str(meta.get("catalog_item_id", "")),
        title=str(game["title"] or game["app_id"]),
    )
    # FRESH fetch — never reuse a stored signed manifest/CDN URL (they expire).
    manifest, cdn_host, cdn_base = await epic_client.fetch_manifest(item)

    total_bytes = sum(c.file_size for c in manifest.chunks)
    await deps.pool.execute_write(
        _EPIC_MANIFEST_UPSERT,
        (game_id, str(manifest.version), len(manifest.chunks), total_bytes, manifest.raw),
    )
    await deps.pool.execute_write(
        "UPDATE games SET size_bytes=? WHERE id=?", (total_bytes, game_id)
    )

    seen: set[str] = set()
    paths: list[str] = []
    for chunk in manifest.chunks:
        p = epic_chunk_path(chunk, manifest.version)
        if p not in seen:
            seen.add(p)
            paths.append(p)

    settings = get_settings()
    result = await epic_prefill_chunks(paths, cdn_host, cdn_base, settings)
    if result.chunks_failed > 0:
        failure_reasons = _summarize_failures(result.failures)
        _log.warning(
            "prefill.epic.chunks_failed",
            job_id=job_id,
            game_id=game_id,
            failed=result.chunks_failed,
            total=result.chunks_total,
            failure_reasons=failure_reasons,
        )
        last_error = (
            f"prefill: {result.chunks_failed}/{result.chunks_total} chunks failed"
            f"{_failure_suffix(failure_reasons)}"
        )[:200]
        await deps.pool.execute_write(
            "UPDATE games SET status='failed', last_error=? WHERE id=?",
            (last_error, game_id),
        )
        raise RuntimeError(
            f"epic prefill failed: {result.chunks_failed}/{result.chunks_total} chunks"
        )

    # Inline header-HIT verification (epic validation; F7-epic disk-stat deferred).
    hit_ratio = await epic_verify_cached(paths[:20], cdn_host, cdn_base, settings)
    if paths and hit_ratio < 0.5:
        _log.warning(
            "prefill.epic.low_hit_ratio",
            job_id=job_id,
            game_id=game_id,
            hit_ratio=round(hit_ratio, 3),
        )
    # F8: Epic prefill always fetches a FRESH manifest (signed URLs expire), so
    # the cache now reflects current_version — adopt it so the scheduled diff
    # stops re-enqueuing this game every cycle.
    await deps.pool.execute_write(
        "UPDATE games SET status='up_to_date', last_prefilled_at=CURRENT_TIMESTAMP, "
        "cached_version=current_version WHERE id=?",
        (game_id,),
    )
    _log.info(
        "prefill.epic.completed",
        job_id=job_id,
        game_id=game_id,
        total=result.chunks_total,
        hit_ratio=round(hit_ratio, 3),
    )


async def _steam_prefill(job: dict[str, Any], deps: Deps) -> None:
    """Prefill one Steam game through the host-installed SteamPrefill binary (F5).

    Sets the game to 'downloading', then delegates to ``_steam_prefill_inner``
    inside a guard so any failure (subprocess death, expired session, network)
    marks the game 'failed' rather than leaving it stuck 'downloading' forever
    (UAT-10 #2; mirrors the Epic path).

    Raises:
        ValueError — unknown game, non-steam platform, or non-numeric app_id.
        RuntimeError — prefill_driver is None, or SteamPrefill exited non-zero.
    """
    if deps.prefill_driver is None:
        raise RuntimeError("prefill_driver is required for prefill handler")
    prefill_driver = deps.prefill_driver
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("prefill job has no game_id")

    game = await deps.pool.read_one(
        "SELECT id, app_id, platform, cached_version, current_version FROM games WHERE id=?",
        (game_id,),
    )
    if game is None:
        raise ValueError(f"game {game_id} not found in games table")
    if game["platform"] != "steam":
        raise ValueError(f"game {game_id} platform is {game['platform']!r}, not steam")

    job_id = job.get("id")
    force = bool(job.get("force", False))
    await deps.pool.execute_write("UPDATE games SET status='downloading' WHERE id=?", (game_id,))
    _log.info("prefill.started", job_id=job_id, game_id=game_id)
    try:
        await _steam_prefill_inner(job_id, game_id, game, deps, prefill_driver, force=force)
    except Exception as e:
        # Never leave the game stuck in 'downloading'. The non-ok-exit path
        # already set 'failed' (this then no-ops via the status guard); any other
        # failure (subprocess/network) is marked here before the re-raise.
        with contextlib.suppress(Exception):
            await deps.pool.execute_write(
                "UPDATE games SET status='failed', last_error=? "
                "WHERE id=? AND status='downloading'",
                (f"prefill: {type(e).__name__}: {e}"[:200], game_id),
            )
        raise


async def _steam_prefill_inner(
    job_id: Any,
    game_id: int,
    game: dict[str, Any],
    deps: Deps,
    prefill_driver: SteamPrefillDriver,
    *,
    force: bool,
) -> None:
    try:
        app_id_int = int(game["app_id"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"game {game_id} app_id not numeric") from e

    result = await prefill_driver.prefill_apps([app_id_int], force=force)
    _log.info(
        "prefill.completed",
        job_id=job_id,
        game_id=game_id,
        app_id=app_id_int,
        ok=result.ok,
    )

    if not result.ok:
        # SteamPrefill exited non-zero. Surface the tail of its output as the
        # operator-facing reason (it never logs token bytes — see the driver).
        last_error = (f"prefill: SteamPrefill exited non-zero: {result.raw[-150:]}")[:200]
        await deps.pool.execute_write(
            "UPDATE games SET status='failed', last_error=? WHERE id=?",
            (last_error, game_id),
        )
        raise RuntimeError(f"steam prefill failed for app {app_id_int} (exit non-zero)")

    # ID5: success → enqueue a validate job (it sets the final status). The
    # jobs.source CHECK allows scheduler/cli/gameshelf/api — use 'scheduler'
    # for this automated enqueue.
    # ON CONFLICT DO NOTHING (audit 2026-06-09): the migration-0006 in-flight
    # UNIQUE index dedups against an already queued/running validate for this
    # game (e.g. an operator-triggered validate, or a duplicate prefill), so we
    # don't pile up redundant validate rows that burn the serial steam slot.
    await deps.pool.execute_write(
        "INSERT INTO jobs (kind, game_id, platform, state, source) "
        "VALUES ('validate', ?, 'steam', 'queued', 'scheduler') ON CONFLICT DO NOTHING",
        (game_id,),
    )
    # F8: full success → what's cached is now the current version. The validate
    # job (enqueued above) will re-affirm this, but recording it here means the
    # scheduled-prefill diff skips this game immediately even before validation.
    await deps.pool.execute_write(
        "UPDATE games SET last_prefilled_at=CURRENT_TIMESTAMP, "
        "cached_version=current_version WHERE id=?",
        (game_id,),
    )
    _log.info("prefill.validate_enqueued", job_id=job_id, game_id=game_id)
