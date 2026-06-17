"""Tests for orchestrator.scheduler.manager (F12)."""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import CapturingLogger

from orchestrator.scheduler import manager as manager_mod
from orchestrator.scheduler.manager import SchedulerManager

pytestmark = pytest.mark.asyncio


def _logged_events(logger: CapturingLogger) -> list[str]:
    """Event names from a CapturingLogger patched in for the module logger.

    We patch the module's ``_log`` directly rather than use
    ``structlog.testing.capture_logs`` because the app configures
    ``cache_logger_on_first_use=True`` — once any earlier test triggers
    ``configure_logging()`` the bound logger is cached and ``capture_logs``
    can no longer intercept it (an order-dependent flake we hit live)."""
    return [call.args[0] for call in logger.calls if call.args]


async def _wait_until_stopped(scheduler: object, timeout_sec: float = 2.0) -> None:
    """AsyncIOScheduler.shutdown() defers the actual stop to a loop callback,
    so ``running`` does not flip synchronously. Poll until it does (or fail
    loudly) so the dangling-stopped precondition is deterministic under load.

    Returning with ``running is False`` also guarantees the deferred
    ``_shutdown`` callback has run, since ``running`` only flips inside it."""
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while scheduler.running:  # type: ignore[attr-defined]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("scheduler did not reach stopped state in time")
        await asyncio.sleep(0.01)


class TestSchedulerManager:
    async def test_disabled_manager_reports_running_false(self, pool):
        mgr = SchedulerManager(
            pool=pool,
            enabled=False,
            library_sync_interval_sec=21600,
        )
        assert mgr.running is False
        # start() on a disabled manager is a no-op
        await mgr.start()
        assert mgr.running is False
        await mgr.shutdown()

    async def test_enabled_manager_starts_and_reports_running(self, pool):
        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
        )
        try:
            await mgr.start()
            assert mgr.running is True
        finally:
            await mgr.shutdown()
        assert mgr.running is False

    async def test_enabled_manager_registers_library_sync_job(self, pool):
        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
        )
        try:
            await mgr.start()
            jobs = mgr.get_registered_job_ids()
            assert "library_sync_steam" in jobs
        finally:
            await mgr.shutdown()

    async def test_disabled_manager_registers_no_jobs(self, pool):
        mgr = SchedulerManager(
            pool=pool,
            enabled=False,
            library_sync_interval_sec=21600,
        )
        await mgr.start()
        assert mgr.get_registered_job_ids() == []
        await mgr.shutdown()

    async def test_sweep_job_registered_when_enabled(self, pool):
        from orchestrator.scheduler.manager import VALIDATION_SWEEP_JOB_ID

        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
            validation_sweep_enabled=True,
            validation_sweep_cron="0 3 * * 0",
        )
        try:
            await mgr.start()
            assert VALIDATION_SWEEP_JOB_ID in mgr.get_registered_job_ids()
        finally:
            await mgr.shutdown()

    async def test_sweep_job_absent_when_disabled(self, pool):
        from orchestrator.scheduler.manager import VALIDATION_SWEEP_JOB_ID

        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
            validation_sweep_enabled=False,
            validation_sweep_cron="0 3 * * 0",
        )
        try:
            await mgr.start()
            ids = mgr.get_registered_job_ids()
            assert VALIDATION_SWEEP_JOB_ID not in ids
            assert "library_sync_steam" in ids  # disabling sweep keeps the scheduler running
        finally:
            await mgr.shutdown()

    async def test_interval_is_passed_through_to_trigger(self, pool):
        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=60,
        )
        try:
            await mgr.start()
            job = mgr.get_job("library_sync_steam")
            assert job is not None
            # IntervalTrigger.interval is a timedelta(seconds=60)
            assert job.trigger.interval.total_seconds() == 60.0
        finally:
            await mgr.shutdown()

    async def test_shutdown_is_idempotent(self, pool):
        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
        )
        await mgr.start()
        await mgr.shutdown()
        # Second shutdown call must not raise
        await mgr.shutdown()

    async def test_start_is_idempotent(self, pool):
        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
        )
        try:
            await mgr.start()
            await mgr.start()  # second call should be a no-op
            assert mgr.running is True
        finally:
            await mgr.shutdown()

    async def test_start_replaces_stale_nonrunning_scheduler_without_leak(self, pool, monkeypatch):
        """SEV-2 (code review 2026-06-02): start()'s idempotency guard only
        short-circuits when the held scheduler is *running*. If `_scheduler`
        is non-None but stopped (e.g. a prior shutdown that raised partway and
        left the field dangling), the old guard fell through and silently
        overwrote `_scheduler` with a fresh instance — abandoning the prior
        object instead of disposing of it. The contract must be: never leak a
        held scheduler — dispose of the stale instance (with a warning) before
        rebuilding."""
        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
        )
        try:
            await mgr.start()
            stale = mgr._scheduler
            assert stale is not None and stale.running

            # Reproduce the dangling precondition: the underlying scheduler is
            # stopped out-of-band, but the manager still references it (exactly
            # what a partial/failed teardown leaves behind).
            stale.shutdown(wait=False)
            await _wait_until_stopped(stale)
            assert mgr._scheduler is stale and not stale.running

            # Spy on the disposal path so the test discriminates "took the
            # dispose-and-replace path" from "silently overwrote + only logged".
            disposed: list[bool] = []
            real_dispose = mgr._dispose_stale_scheduler

            def spy_dispose() -> None:
                disposed.append(True)
                real_dispose()

            monkeypatch.setattr(mgr, "_dispose_stale_scheduler", spy_dispose)
            rec = CapturingLogger()
            monkeypatch.setattr(manager_mod, "_log", rec)
            await mgr.start()

            # A fresh, running scheduler replaced the stale one...
            assert mgr.running is True
            assert mgr._scheduler is not stale
            # ...the disposal path actually ran (not just a logged overwrite)...
            assert disposed == [True]
            # ...and the replacement was acknowledged, not a silent leak.
            assert "scheduler.replacing_stale_instance" in _logged_events(rec)
        finally:
            await mgr.shutdown()

    async def test_start_recovers_after_dangling_scheduler(self, pool):
        """End-to-end recovery: after a stale/dangling scheduler is left in
        place, a subsequent start() yields a healthy manager that registers
        its jobs and reports running — proving the dispose-and-rebuild path
        produces a fully functional scheduler, not just a cleared field."""
        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=60,
        )
        try:
            await mgr.start()
            stale = mgr._scheduler
            assert stale is not None
            stale.shutdown(wait=False)
            await _wait_until_stopped(stale)
            assert not mgr.running

            await mgr.start()

            assert mgr.running is True
            assert "library_sync_steam" in mgr.get_registered_job_ids()
            job = mgr.get_job("library_sync_steam")
            assert job is not None
            assert job.trigger.interval.total_seconds() == 60.0
        finally:
            await mgr.shutdown()

    async def test_concurrent_start_after_stale_does_not_leak(self, pool, monkeypatch):
        """Concurrency guard: two concurrent start() calls after a dangling
        scheduler must converge on exactly ONE running scheduler — never two
        started-but-abandoned instances. The lock (serialization) plus the
        synchronous dispose (no await between guard and assignment) guarantee
        the rebuild is atomic; this test pins that contract against regressions
        (e.g. someone later introducing an await without the lock)."""
        created: list[object] = []
        real_cls = manager_mod.AsyncIOScheduler

        def tracking_factory(*args: object, **kwargs: object) -> object:
            inst = real_cls(*args, **kwargs)
            created.append(inst)
            return inst

        monkeypatch.setattr(manager_mod, "AsyncIOScheduler", tracking_factory)

        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
        )
        try:
            await mgr.start()
            stale = mgr._scheduler
            assert stale is not None
            stale.shutdown(wait=False)
            await _wait_until_stopped(stale)
            assert not mgr.running

            # Race two rebuilds.
            await asyncio.gather(mgr.start(), mgr.start())

            assert mgr.running is True
            running = [s for s in created if s.running]  # type: ignore[attr-defined]
            assert len(running) == 1, "concurrent start() leaked a running scheduler"
            assert running[0] is mgr._scheduler
        finally:
            await mgr.shutdown()


class TestScheduledPrefillRegistration:
    async def test_registers_scheduled_prefill_job(self, pool):
        from orchestrator.scheduler.manager import SCHEDULED_PREFILL_JOB_ID

        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
            validation_sweep_enabled=False,
            scheduled_prefill_enabled=True,
        )
        await mgr.start()
        try:
            assert SCHEDULED_PREFILL_JOB_ID in mgr.get_registered_job_ids()
        finally:
            await mgr.shutdown()

    async def test_scheduled_prefill_disabled_not_registered(self, pool):
        from orchestrator.scheduler.manager import SCHEDULED_PREFILL_JOB_ID

        mgr = SchedulerManager(
            pool=pool,
            enabled=True,
            library_sync_interval_sec=21600,
            validation_sweep_enabled=False,
            scheduled_prefill_enabled=False,
        )
        await mgr.start()
        try:
            assert SCHEDULED_PREFILL_JOB_ID not in mgr.get_registered_job_ids()
        finally:
            await mgr.shutdown()
