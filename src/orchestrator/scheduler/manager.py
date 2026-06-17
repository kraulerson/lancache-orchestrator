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

import asyncio
from typing import TYPE_CHECKING

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from orchestrator.scheduler.jobs import (
    enqueue_library_sync,
    enqueue_scheduled_prefill,
    enqueue_validation_sweep,
)

if TYPE_CHECKING:
    from apscheduler.job import Job

    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)


# Stable job IDs (F12 D6): manager re-registers on every boot. Using
# consistent ids means `replace_existing=True` lets us re-deploy without
# orphan jobs if the in-memory store ever gets persisted in a future BL.
LIBRARY_SYNC_JOB_ID = "library_sync_steam"
VALIDATION_SWEEP_JOB_ID = "validation_sweep"
SCHEDULED_PREFILL_JOB_ID = "scheduled_prefill"


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
        validation_sweep_enabled: bool = True,
        validation_sweep_cron: str = "0 3 * * 0",
        scheduled_prefill_enabled: bool = True,
    ) -> None:
        self._pool = pool
        self._enabled = enabled
        self._library_sync_interval_sec = library_sync_interval_sec
        self._validation_sweep_enabled = validation_sweep_enabled
        self._validation_sweep_cron = validation_sweep_cron
        self._scheduled_prefill_enabled = scheduled_prefill_enabled
        self._scheduler: AsyncIOScheduler | None = None
        # Serializes start()/shutdown() so the lifecycle stays atomic even if
        # they are ever called concurrently (e.g. a future restart endpoint).
        # Uncontended acquire does not suspend, so the single sequential
        # lifespan path is unaffected.
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        """True iff the underlying APScheduler is running. Drives
        `/health.scheduler_running`."""
        return self._scheduler is not None and self._scheduler.running

    async def start(self) -> None:
        """Construct the AsyncIOScheduler, register jobs, start running.
        No-op when disabled or already running (F12 D11).

        If a previous, non-running scheduler is still held (e.g. a prior
        shutdown that raised partway and left ``_scheduler`` dangling), it is
        disposed of before a fresh instance is built — the manager never
        silently abandons a scheduler it owns (SEV-2, code review 2026-06-02).
        """
        if not self._enabled:
            _log.info("scheduler.disabled_by_settings")
            return
        async with self._lock:
            if self._scheduler is not None:
                if self._scheduler.running:
                    return
                # Non-None but stopped: a partial/failed teardown left the
                # field dangling. Dispose of it so we replace, not leak.
                _log.warning("scheduler.replacing_stale_instance")
                self._dispose_stale_scheduler()

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

            if self._validation_sweep_enabled:
                scheduler.add_job(
                    enqueue_validation_sweep,
                    trigger=CronTrigger.from_crontab(self._validation_sweep_cron, timezone="UTC"),
                    args=(self._pool,),
                    id=VALIDATION_SWEEP_JOB_ID,
                    name="Enqueue validation sweep",
                    replace_existing=True,
                )

            if self._scheduled_prefill_enabled:
                # F8: same cadence as library sync, independent of it (no
                # completion-chaining) — eventually consistent: a fresh patch is
                # enqueued once both the next sync (refreshing current_version)
                # and the next prefill diff have run.
                scheduler.add_job(
                    enqueue_scheduled_prefill,
                    trigger=IntervalTrigger(seconds=self._library_sync_interval_sec),
                    args=(self._pool,),
                    id=SCHEDULED_PREFILL_JOB_ID,
                    name="Enqueue scheduled prefill (version-diff)",
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
        started or already stopped. Serialized against start() via the lock."""
        async with self._lock:
            if self._scheduler is None:
                return
            if self._scheduler.running:
                # wait=True drains in-flight callbacks; our callbacks are fast.
                self._scheduler.shutdown(wait=True)
            self._scheduler = None
            _log.info("scheduler.stopped")

    def _dispose_stale_scheduler(self) -> None:
        """Tear down the held-but-stopped scheduler before a fresh one is built.
        Clears the reference unconditionally so ``start()`` can always proceed;
        if the instance is somehow still running (defensive — the caller only
        reaches here on the non-running path), it is stopped first. Best-effort:
        a half-torn-down instance may raise on shutdown — swallow it rather than
        block restart.

        Intentionally synchronous: ``start()`` must hold no ``await`` between its
        guard check and the ``self._scheduler = scheduler`` assignment so that,
        together with the lock, the rebuild stays atomic and concurrent callers
        can never race to construct two schedulers.
        """
        stale = self._scheduler
        self._scheduler = None
        try:
            if stale is not None and stale.running:
                stale.shutdown(wait=False)
        except Exception as e:  # pragma: no cover - defensive
            _log.warning("scheduler.stale_dispose_failed", reason=str(e)[:200])

    # --- introspection helpers (used by tests + future status page) ---

    def get_registered_job_ids(self) -> list[str]:
        if self._scheduler is None:
            return []
        return [j.id for j in self._scheduler.get_jobs()]

    def get_job(self, job_id: str) -> Job | None:
        if self._scheduler is None:
            return None
        return self._scheduler.get_job(job_id)
