"""Startup reaper for orphaned `running` jobs (ID6).

Any `jobs` row with `state='running'` at orchestrator startup is by
definition orphaned: the worker process that claimed it died with the
previous orchestrator process (the jobs worker runs inside the same
container; it cannot survive a container restart). The reaper marks
those rows `failed` atomically BEFORE the new jobs worker starts
polling — otherwise the same job would be claim-by-current-worker'd
or stay stuck `running` forever, depending on the handler.

Surfaced by UAT-6 deployment-shape audit (finding F-UAT6-8) plus
FRD §5 ID6 requirement. Called from `_lifespan` after pool init and
before `jobs_worker_task` is spawned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)

REAPER_ERROR_MESSAGE = "orchestrator restarted while job was running (ID6 reaper)"


async def reap_running_jobs(pool: Pool) -> int:
    """Mark every `state='running'` job as `failed` with a uniform error
    message. Returns the number of rows touched.

    Logs at WARN when reaping > 0 rows (surface that the previous process
    didn't shut down cleanly) and at INFO when there's nothing to reap
    (the normal, no-prior-crash case).
    """
    rowcount = await pool.execute_write(
        "UPDATE jobs SET state='failed', finished_at=CURRENT_TIMESTAMP, error=? "
        "WHERE state='running'",
        (REAPER_ERROR_MESSAGE,),
    )
    if rowcount > 0:
        _log.warning("jobs.reaper.reaped_orphans", count=rowcount)
    else:
        _log.info("jobs.reaper.no_orphans")
    return rowcount


GAME_REAPER_ERROR_MESSAGE = (
    "prefill interrupted (orchestrator restart or job timeout) — re-run prefill"
)


async def reap_orphaned_game_status(pool: Pool) -> int:
    """Record the interruption on games left in the legacy ``'downloading'`` status.

    Prefill no longer writes ``'downloading'`` into ``games.status`` at all — an
    in-flight job is the ``jobs`` row (``state='running'``), and status is cache
    truth written only by ``jobs.measurement.record_measurement``. So the rows
    this touches are historical: games stranded by a restart or a job timeout
    BEFORE the 2026-09-04 split, which nothing else would ever explain.

    It therefore stamps the job outcome and leaves status alone. Cache truth is
    restored by the next measurement of the row
    (``jobs.measurement.record_measurement``), never by this code guessing from a
    job's fate — and the scheduled sweep now reaches every such row unconditionally,
    since its candidate SQL (Task 5) carries no status filter. Run at boot AFTER
    ``reap_running_jobs``. Returns rows touched.

    ``last_error`` carries the same text, mirroring ``record_job_outcome`` for
    the API/CLI readers that still consume that legacy column.
    """
    rowcount = await pool.execute_write(
        "UPDATE games SET last_job_outcome=?, last_job_outcome_at=CURRENT_TIMESTAMP, "
        "last_error=? WHERE status='downloading'",
        (GAME_REAPER_ERROR_MESSAGE, GAME_REAPER_ERROR_MESSAGE),
    )
    if rowcount > 0:
        _log.warning("jobs.reaper.reaped_orphan_downloading_games", count=rowcount)
    return rowcount
