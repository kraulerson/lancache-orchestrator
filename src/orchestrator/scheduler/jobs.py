"""Scheduled job callbacks (F12 D6 — scheduler enqueues, jobs worker executes).

These are async functions invoked by APScheduler. They MUST NOT raise
— a raised exception puts APScheduler into a degraded state and we
want failed enqueues to be best-effort (next fire will retry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)


async def enqueue_library_sync(pool: Pool) -> int:
    """Insert a `library_sync` job row if none is queued/running for steam.

    Returns the rowcount affected (1 if a new row was queued, 0 if a dedup
    conflict skipped it or on DB failure). Never raises — DB errors are logged
    and swallowed so a failing scheduler tick doesn't crash the scheduler.

    Dedup is DB-enforced: the partial UNIQUE index `idx_jobs_library_sync_inflight`
    (migration 0004) guarantees at most one queued/running library_sync per
    platform, and `ON CONFLICT DO NOTHING` collapses a concurrent cron + API
    race into a single row (previously an app-level SELECT-then-INSERT that
    straddled an await and could double-insert — code review 2026-06-02). F12 D7.
    """
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('library_sync', 'steam', 'queued', 'scheduler') "
            "ON CONFLICT DO NOTHING"
        )
        if inserted:
            _log.info("scheduler.library_sync.queued")
        else:
            _log.info("scheduler.library_sync.dedup_skip")
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


async def enqueue_validation_sweep(pool: Pool) -> int:
    """Insert a `sweep` job row if none is queued/running (F13).

    Mirrors `enqueue_library_sync`: at most one in-flight sweep, DB-enforced by
    `idx_jobs_sweep_inflight` (migration 0005) via `ON CONFLICT DO NOTHING`.
    Returns the rowcount (1 queued / 0 deduped-or-failed). Never raises — a
    failing scheduler tick must not degrade APScheduler. The sweep is not
    platform-scoped, so `platform` is left NULL.
    """
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, state, source) "
            "VALUES ('sweep', 'queued', 'scheduler') ON CONFLICT DO NOTHING"
        )
        if inserted:
            _log.info("scheduler.sweep.queued")
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


async def enqueue_scheduled_prefill(pool: Pool) -> int:
    """Enqueue 'prefill' jobs for owned games that are new, version-diverged, or
    validation_failed — and not block-listed (F8 scheduled prefill driver).

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
            "WHERE g.owned = 1 "
            "  AND (g.cached_version IS NULL "
            "       OR g.cached_version <> g.current_version "
            "       OR g.status = 'validation_failed') "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM block_list b "
            "      WHERE b.platform = g.platform AND b.app_id = g.app_id) "
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
