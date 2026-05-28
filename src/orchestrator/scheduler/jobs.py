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

    Returns the rowcount affected (0 if dedup-skipped or DB failure, 1
    if a new row was queued). Never raises — DB errors are logged and
    swallowed so a failing scheduler tick doesn't crash the scheduler.

    Dedup logic mirrors `sync.trigger_library_sync` so cron + operator
    triggers don't race onto duplicate rows. F12 D7.
    """
    try:
        existing = await pool.read_one(
            "SELECT id FROM jobs "
            "WHERE kind='library_sync' AND platform='steam' "
            "AND state IN ('queued','running') "
            "ORDER BY id LIMIT 1"
        )
        if existing is not None:
            _log.info("scheduler.library_sync.dedup_skip", existing_job_id=existing["id"])
            return 0

        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('library_sync', 'steam', 'queued', 'scheduler')"
        )
        _log.info("scheduler.library_sync.queued")
        return 1
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
