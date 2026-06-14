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
    """Reset games stuck in the transient ``'downloading'`` status to ``'failed'``.

    A prefill sets the game ``'downloading'`` and resets it on completion or
    failure — but the per-job max-runtime timeout cancels the handler via
    ``CancelledError`` (a ``BaseException``), which bypasses the handler's
    ``except Exception`` reset, and a hard crash skips it entirely. Either way the
    game is left ``'downloading'`` forever with nothing to recover it (UAT-11
    F-INT-1). Run this at boot AFTER ``reap_running_jobs`` — no prefill is in
    flight then, so any ``'downloading'`` game is genuinely orphaned. Returns the
    number of rows touched.
    """
    rowcount = await pool.execute_write(
        "UPDATE games SET status='failed', last_error=? WHERE status='downloading'",
        (GAME_REAPER_ERROR_MESSAGE,),
    )
    if rowcount > 0:
        _log.warning("jobs.reaper.reaped_orphan_downloading_games", count=rowcount)
    return rowcount
