"""Tests for orchestrator.scheduler.manager (F12)."""

from __future__ import annotations

import pytest

from orchestrator.scheduler.manager import SchedulerManager

pytestmark = pytest.mark.asyncio


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
