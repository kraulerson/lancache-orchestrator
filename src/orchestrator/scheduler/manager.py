"""SchedulerManager — wraps APScheduler 3.x AsyncIOScheduler (F12).

The manager:
- Constructs an `AsyncIOScheduler` with sensible defaults (in-memory
  job store, asyncio executor, max_instances=1, no misfire grace)
- Registers configured periodic jobs at startup
- Exposes `.running` for `/health.scheduler_running`
- Provides idempotent `.start()` / `.shutdown()` for lifespan integration

The scheduler itself does not run business logic — it invokes thin
"enqueue" callbacks in `jobs.py` that insert rows into the `jobs`
table. The BL11 jobs worker picks them up (F12 D6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from orchestrator.scheduler.jobs import enqueue_library_sync

if TYPE_CHECKING:
    from apscheduler.job import Job

    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)


# Stable job IDs (F12 D6): manager re-registers on every boot. Using
# consistent ids means `replace_existing=True` lets us re-deploy without
# orphan jobs if the in-memory store ever gets persisted in a future BL.
LIBRARY_SYNC_JOB_ID = "library_sync_steam"


class SchedulerManager:
    """Async scheduler facade. Idempotent start/shutdown for lifespan use.

    Disable via `enabled=False` (Settings.scheduler_enabled). A disabled
    manager reports `.running=False`, registers no jobs, and `.start()`
    is a no-op — operator-facing `/health.scheduler_running` correctly
    surfaces False, returning 503.
    """

    def __init__(
        self,
        *,
        pool: Pool,
        enabled: bool,
        library_sync_interval_sec: int,
    ) -> None:
        self._pool = pool
        self._enabled = enabled
        self._library_sync_interval_sec = library_sync_interval_sec
        self._scheduler: AsyncIOScheduler | None = None

    @property
    def running(self) -> bool:
        """True iff the underlying APScheduler is running. Drives
        `/health.scheduler_running`."""
        return self._scheduler is not None and self._scheduler.running

    async def start(self) -> None:
        """Construct the AsyncIOScheduler, register jobs, start running.
        No-op when disabled or already started (F12 D11)."""
        if not self._enabled:
            _log.info("scheduler.disabled_by_settings")
            return
        if self._scheduler is not None and self._scheduler.running:
            return

        scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={
                # D4: only one fire at a time per job
                "max_instances": 1,
                # D5: always fire on next opportunity, even if late
                "misfire_grace_time": None,
                # Don't coalesce missed fires into a burst — single replay
                "coalesce": True,
            },
        )

        scheduler.add_job(
            enqueue_library_sync,
            trigger=IntervalTrigger(seconds=self._library_sync_interval_sec),
            args=(self._pool,),
            id=LIBRARY_SYNC_JOB_ID,
            name="Enqueue library_sync for steam",
            replace_existing=True,
        )

        scheduler.start()
        self._scheduler = scheduler
        _log.info(
            "scheduler.started",
            library_sync_interval_sec=self._library_sync_interval_sec,
            jobs=len(scheduler.get_jobs()),
        )

    async def shutdown(self) -> None:
        """Stop the scheduler. Idempotent — safe to call when not
        started or already stopped."""
        if self._scheduler is None:
            return
        if self._scheduler.running:
            # wait=True drains in-flight callbacks; our callbacks are fast.
            self._scheduler.shutdown(wait=True)
        self._scheduler = None
        _log.info("scheduler.stopped")

    # --- introspection helpers (used by tests + future status page) ---

    def get_registered_job_ids(self) -> list[str]:
        if self._scheduler is None:
            return []
        return [j.id for j in self._scheduler.get_jobs()]

    def get_job(self, job_id: str) -> Job | None:
        if self._scheduler is None:
            return None
        return self._scheduler.get_job(job_id)
