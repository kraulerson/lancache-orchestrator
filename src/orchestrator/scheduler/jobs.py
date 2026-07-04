"""Scheduled job callbacks (F12 D6 — scheduler enqueues, jobs worker executes).

These are async functions invoked by APScheduler. They MUST NOT raise
— a raised exception puts APScheduler into a degraded state and we
want failed enqueues to be best-effort (next fire will retry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from orchestrator.db.pool import PoolError
from orchestrator.platform.steam.selection_file import as_int

if TYPE_CHECKING:
    from orchestrator.clients.agent_client import AgentClient
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)


async def enqueue_library_sync(pool: Pool, platform: str = "steam") -> int:
    """Insert a `library_sync` job row if none is queued/running for ``platform``.

    Returns the rowcount affected (1 if a new row was queued, 0 if a dedup
    conflict skipped it or on DB failure). Never raises — DB errors are logged
    and swallowed so a failing scheduler tick doesn't crash the scheduler.

    Dedup is DB-enforced: the partial UNIQUE index `idx_jobs_library_sync_inflight`
    (migration 0004) guarantees at most one queued/running library_sync per
    platform, and `ON CONFLICT DO NOTHING` collapses a concurrent cron + API
    race into a single row (previously an app-level SELECT-then-INSERT that
    straddled an await and could double-insert — code review 2026-06-02). F12 D7.
    Registered on the cron for both steam and epic (Piece 2 — the orchestrator
    owns Epic enumeration since EpicPrefill never auto-downloads new games).
    """
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('library_sync', ?, 'queued', 'scheduler') "
            "ON CONFLICT DO NOTHING",
            (platform,),
        )
        if inserted:
            _log.info("scheduler.library_sync.queued", platform=platform)
        else:
            _log.info("scheduler.library_sync.dedup_skip", platform=platform)
        return inserted
    except PoolError as e:
        _log.error("scheduler.library_sync.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        # Defensive: any other exception (callback shouldn't crash the
        # scheduler) is logged at ERROR and swallowed.
        _log.error(
            "scheduler.library_sync.unexpected_error",
            error=type(e).__name__,
            reason=str(e)[:200],
        )
        return 0


async def enqueue_validation_sweep(
    pool: Pool, *, full: bool = False, source: str = "scheduler"
) -> int:
    """Insert a `sweep` job row if none is queued/running (F13).

    ``full=True`` validates EVERY game across all platforms (the validate-all
    backfill), carried
    on the job payload `{"full": true}`; the weekly cron uses the default
    (status-gated) sweep. Mirrors `enqueue_library_sync`: at most one in-flight
    sweep, DB-enforced by `idx_jobs_sweep_inflight` (migration 0005) via
    `ON CONFLICT DO NOTHING`. Returns the rowcount (1 queued / 0 deduped-or-failed).
    Never raises — a failing scheduler tick must not degrade APScheduler. The
    sweep is not platform-scoped, so `platform` is left NULL.
    """
    payload = '{"full": true}' if full else None
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, state, source, payload) "
            "VALUES ('sweep', 'queued', ?, ?) ON CONFLICT DO NOTHING",
            (source, payload),
        )
        if inserted:
            _log.info("scheduler.sweep.queued", full=full, source=source)
        else:
            _log.info("scheduler.sweep.dedup_skip")
        return inserted
    except PoolError as e:
        _log.error("scheduler.sweep.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        _log.error(
            "scheduler.sweep.unexpected_error",
            error=type(e).__name__,
            reason=str(e)[:200],
        )
        return 0


async def enqueue_fetch_manifests(pool: Pool, *, source: str = "scheduler") -> int:
    """Insert a ``fetch_manifests`` job if none is queued/running.

    Mirrors ``enqueue_validation_sweep``: at most one in-flight fetch_manifests,
    DB-enforced via the ``idx_jobs_fetch_manifests_inflight`` partial-unique index
    (migration 0009) + ``ON CONFLICT DO NOTHING``. Returns the rowcount (1 queued
    / 0 deduped). Never raises — a failing scheduler tick must not degrade
    APScheduler.
    """
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, state, source) "
            "VALUES ('fetch_manifests', 'queued', ?) ON CONFLICT DO NOTHING",
            (source,),
        )
        if inserted:
            _log.info("scheduler.fetch_manifests.queued", source=source)
        else:
            _log.info("scheduler.fetch_manifests.dedup_skip")
        return inserted
    except PoolError as e:
        _log.error("scheduler.fetch_manifests.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        _log.error(
            "scheduler.fetch_manifests.unexpected_error",
            error=type(e).__name__,
            reason=str(e)[:200],
        )
        return 0


async def enqueue_scheduled_prefill(pool: Pool) -> int:
    """Enqueue 'prefill' jobs for owned EPIC games that are new, version-diverged,
    or validation_failed — and not block-listed / prefill-excluded (F8 driver,
    Epic-scoped per Piece 2).

    Steam is prefilled by the host SteamPrefill cron (it auto-grabs recent
    purchases); EpicPrefill never auto-downloads new games, so the orchestrator
    owns Epic. The `platform = 'epic'` filter avoids double-prefilling Steam.

    One bulk INSERT...SELECT. `ON CONFLICT DO NOTHING` + the migration-0006
    in-flight UNIQUE index dedups against a prefill already queued/running for a
    game. The `cached_version IS NULL` disjunct makes the `<>` comparison
    NULL-safe (a never-cached game is caught by the IS NULL arm). Returns the
    number of rows enqueued. Never raises — a failing scheduler tick must not
    degrade APScheduler.
    """
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "SELECT 'prefill', g.id, g.platform, 'queued', 'scheduler' "
            "FROM games g "
            # Piece 2: the orchestrator's scheduled prefill covers EPIC ONLY.
            # Steam is prefilled by the host SteamPrefill cron (which auto-grabs
            # recent purchases); EpicPrefill never auto-downloads new games, so
            # the orchestrator owns Epic. Scoping to epic avoids double-prefilling
            # every Steam game.
            "WHERE g.owned = 1 AND g.platform = 'epic' "
            "  AND (g.cached_version IS NULL "
            "       OR g.cached_version <> g.current_version "
            "       OR g.status = 'validation_failed') "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM block_list b "
            "      WHERE b.platform = g.platform AND b.app_id = g.app_id) "
            # #225: skip games auto-excluded as non-games (or operator-excluded).
            # An 'allow' override row does NOT match, so it never suppresses prefill.
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM prefill_exclusions e "
            "      WHERE e.platform = g.platform AND e.app_id = g.app_id "
            "        AND e.mode = 'exclude') "
            "ON CONFLICT DO NOTHING"
        )
        _log.info("scheduler.scheduled_prefill.enqueued", count=inserted)
        return inserted
    except PoolError as e:
        _log.error("scheduler.scheduled_prefill.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        _log.error(
            "scheduler.scheduled_prefill.unexpected_error",
            error=type(e).__name__,
            reason=str(e)[:200],
        )
        return 0


async def enqueue_auto_classify_block(pool: Pool, agent_client: AgentClient | None = None) -> int:
    """Auto-exclude non-games from FUTURE scheduled prefill, AFTER they've been
    downloaded once (#225/#366).

    The scheduled prefill keeps caching everything. This step runs over owned
    Steam games that have been prefilled at least once (`last_prefilled_at` set),
    classifies each by its Steam store type/name (`steam_app_info`, populated by
    library_sync) via the #229 selection classifier, and inserts an 'exclude' row
    into `prefill_exclusions` for soundtracks / tools / SDKs / dedicated servers /
    demos. The next prefill cycle then skips them. `ON CONFLICT DO NOTHING` +
    the `NOT EXISTS` filter mean an operator 'allow' override is never overwritten
    and an already-excluded game isn't re-processed (idempotent).

    Only Steam is classified (`steam_app_info` is Steam-only). A game Steam types
    as `game` — including a multiplayer-only title — is never auto-excluded.
    Returns the number of rows newly excluded. Never raises (scheduler callback).
    """
    from orchestrator.platform.steam.selection_classifier import classify

    try:
        rows = await pool.read_all(
            "SELECT g.platform AS platform, g.app_id AS app_id, "
            "       sai.app_type AS app_type, sai.name AS name, "
            "       sai.has_single_player AS has_single_player, "
            "       sai.has_multiplayer AS has_multiplayer "
            "FROM games g "
            "JOIN steam_app_info sai ON sai.app_id = g.app_id "
            "WHERE g.owned = 1 AND g.platform = 'steam' "
            "  AND g.last_prefilled_at IS NOT NULL "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM prefill_exclusions e "
            "      WHERE e.platform = g.platform AND e.app_id = g.app_id)"
        )
    except PoolError as e:
        _log.error("scheduler.auto_classify_block.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        _log.error(
            "scheduler.auto_classify_block.read_error",
            error=type(e).__name__,
            reason=str(e)[:200],
        )
        return 0

    inserted = 0
    for row in rows:
        reason = classify(
            row["app_type"],
            row["name"],
            has_single_player=row["has_single_player"],
            has_multiplayer=row["has_multiplayer"],
        )
        if reason is None:
            continue
        try:
            inserted += await pool.execute_write(
                "INSERT INTO prefill_exclusions (platform, app_id, mode, reason, source) "
                "VALUES (?, ?, 'exclude', ?, 'classifier') "
                "ON CONFLICT(platform, app_id) DO NOTHING",
                (row["platform"], row["app_id"], f"auto-classify: {reason}"),
            )
        except PoolError as e:
            _log.error(
                "scheduler.auto_classify_block.insert_error",
                app_id=row["app_id"],
                reason=str(e)[:200],
            )
    if inserted:
        _log.info("scheduler.auto_classify_block.excluded", count=inserted)

    # Piece 1 (Steam): actuate — prune SteamPrefill's selectedAppsToPrefill.json on
    # the agent so the HOST prefill cron stops caching the excluded non-games (the
    # orchestrator's own scheduled prefill is not the active Steam driver). The DB
    # exclusions are the source of truth; a failed prune just retries next tick.
    # Sends the FULL steam exclude + allow sets each run so the file converges and
    # an operator 'allow' re-adds a game. Best-effort; never raises.
    if agent_client is not None:
        try:
            excl = await pool.read_all(
                "SELECT app_id FROM prefill_exclusions "
                "WHERE platform = 'steam' AND mode = 'exclude'"
            )
            allow = await pool.read_all(
                "SELECT app_id FROM prefill_exclusions WHERE platform = 'steam' AND mode = 'allow'"
            )
            exclude_ids = [i for i in (as_int(r["app_id"]) for r in excl) if i is not None]
            restore_ids = [i for i in (as_int(r["app_id"]) for r in allow) if i is not None]
            if exclude_ids or restore_ids:
                res = await agent_client.prune_steam_selection(exclude_ids, restore_ids)
                _log.info("scheduler.auto_classify_block.pruned", **res)
        except Exception as e:
            _log.error(
                "scheduler.auto_classify_block.prune_failed",
                error=type(e).__name__,
                reason=str(e)[:200],
            )
    return inserted
