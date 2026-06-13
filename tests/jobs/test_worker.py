"""Tests for orchestrator.jobs.worker — generic asyncio dispatcher (BL11)."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.jobs.handlers import HANDLERS, clear, register
from orchestrator.jobs.worker import (
    Deps,
    claim_next_job,
    mark_failed,
    mark_succeeded,
    worker_loop,
)

pytestmark = pytest.mark.asyncio


async def _queue_job(pool, kind="library_sync", platform="steam"):
    await pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) VALUES (?, ?, 'queued', 'api')",
        (kind, platform),
    )


class TestClaimNextJob:
    async def test_returns_none_when_empty(self, pool):
        assert await claim_next_job(pool) is None

    async def test_returns_queued_job_with_state_running(self, pool):
        await _queue_job(pool)
        row = await claim_next_job(pool)
        assert row is not None
        assert row["kind"] == "library_sync"
        assert row["state"] == "running"
        assert row["started_at"] is not None

    async def test_skips_already_claimed_jobs(self, pool):
        # prefill: no single-in-flight constraint (unlike library_sync, capped
        # at one per platform by migration 0004), so two can coexist queued.
        await _queue_job(pool, kind="prefill")
        await _queue_job(pool, kind="prefill")
        first = await claim_next_job(pool)
        second = await claim_next_job(pool)
        assert first is not None and second is not None
        assert first["id"] != second["id"]
        # A third claim on now-empty queue → None.
        assert await claim_next_job(pool) is None

    async def test_picks_oldest_first(self, pool):
        await _queue_job(pool, kind="library_sync")
        await _queue_job(pool, kind="prefill")
        first = await claim_next_job(pool)
        assert first is not None
        assert first["kind"] == "library_sync"

    async def test_atomic_under_concurrency(self, pool):
        """Two parallel claim_next_job calls must return distinct rows."""
        for _ in range(4):
            await _queue_job(pool, kind="prefill")
        results = await asyncio.gather(
            claim_next_job(pool),
            claim_next_job(pool),
            claim_next_job(pool),
            claim_next_job(pool),
        )
        ids = {r["id"] for r in results if r is not None}
        assert len(ids) == 4


class TestMarkSucceededFailed:
    async def test_mark_succeeded(self, pool):
        await _queue_job(pool)
        row = await claim_next_job(pool)
        assert row is not None
        await mark_succeeded(pool, int(row["id"]))
        after = await pool.read_one(
            "SELECT state, finished_at, error FROM jobs WHERE id=?", (row["id"],)
        )
        assert after["state"] == "succeeded"
        assert after["finished_at"] is not None
        assert after["error"] is None

    async def test_mark_failed_truncates_to_200(self, pool):
        await _queue_job(pool)
        row = await claim_next_job(pool)
        assert row is not None
        await mark_failed(pool, int(row["id"]), "x" * 500)
        after = await pool.read_one("SELECT state, error FROM jobs WHERE id=?", (row["id"],))
        assert after["state"] == "failed"
        assert len(after["error"]) == 200

    async def test_mark_succeeded_only_affects_running(self, pool):
        # Queued (no started_at) — mark_succeeded should NOT promote it.
        await _queue_job(pool)
        await mark_succeeded(pool, 1)
        after = await pool.read_one("SELECT state FROM jobs WHERE id=1")
        assert after["state"] == "queued"

    async def test_mark_succeeded_retries_transient_pool_error(self, pool, monkeypatch):
        """A transient pool error on the status write must be retried — not leave
        the job stuck 'running' for the next-boot reaper to mislabel 'failed'
        (audit 2026-06-09)."""
        import orchestrator.jobs.worker as wk
        from orchestrator.db.pool import PoolError

        await _queue_job(pool)
        row = await claim_next_job(pool)
        assert row is not None
        job_id = int(row["id"])

        real_write = pool.execute_write
        calls = {"n": 0}

        async def flaky(sql, params=()):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PoolError("transient connection loss")
            return await real_write(sql, params)

        async def _noop(_seconds):
            return None

        monkeypatch.setattr(pool, "execute_write", flaky)
        monkeypatch.setattr(wk.asyncio, "sleep", _noop)

        await mark_succeeded(pool, job_id)  # must not raise — retried to success
        assert calls["n"] == 3

        after = await pool.read_one("SELECT state FROM jobs WHERE id=?", (job_id,))
        assert after["state"] == "succeeded"


class TestWorkerLoopDispatch:
    async def test_dispatches_to_registered_handler(self, pool):
        called: list[int] = []

        async def my_handler(row, deps):
            called.append(int(row["id"]))

        clear()
        register("library_sync", my_handler)

        await _queue_job(pool)

        shutdown = asyncio.Event()
        deps = Deps(pool=pool, steam_client=None)

        async def stopper():
            for _ in range(100):
                if called:
                    break
                await asyncio.sleep(0.01)
            shutdown.set()

        await asyncio.gather(
            worker_loop(deps, shutdown=shutdown, poll_interval_sec=0.02),
            stopper(),
        )
        assert called == [1]
        after = await pool.read_one("SELECT state FROM jobs WHERE id=1")
        assert after["state"] == "succeeded"

    async def test_unknown_kind_marked_failed(self, pool):
        clear()
        # Use 'sweep' — a valid kind per CHECK constraint that has no handler registered.
        await _queue_job(pool, kind="sweep")

        shutdown = asyncio.Event()
        deps = Deps(pool=pool, steam_client=None)

        async def stopper():
            for _ in range(100):
                row = await pool.read_one("SELECT state FROM jobs WHERE id=1")
                if row and row["state"] == "failed":
                    break
                await asyncio.sleep(0.01)
            shutdown.set()

        await asyncio.gather(
            worker_loop(deps, shutdown=shutdown, poll_interval_sec=0.02),
            stopper(),
        )
        row = await pool.read_one("SELECT state, error FROM jobs WHERE id=1")
        assert row["state"] == "failed"
        assert "no handler for kind 'sweep'" in row["error"]

    async def test_handler_crash_does_not_kill_loop(self, pool):
        crashed: list[int] = []
        ran: list[int] = []

        async def crasher(row, deps):
            crashed.append(int(row["id"]))
            raise RuntimeError("kaboom")

        async def good(row, deps):
            ran.append(int(row["id"]))

        clear()
        register("library_sync", crasher)
        register("prefill", good)

        await _queue_job(pool, kind="library_sync")
        await _queue_job(pool, kind="prefill")

        shutdown = asyncio.Event()
        deps = Deps(pool=pool, steam_client=None)

        async def stopper():
            for _ in range(200):
                if ran:
                    break
                await asyncio.sleep(0.01)
            shutdown.set()

        await asyncio.gather(
            worker_loop(deps, shutdown=shutdown, poll_interval_sec=0.02),
            stopper(),
        )
        assert crashed == [1]
        assert ran == [2]
        row1 = await pool.read_one("SELECT state, error FROM jobs WHERE id=1")
        row2 = await pool.read_one("SELECT state FROM jobs WHERE id=2")
        assert row1["state"] == "failed"
        assert "RuntimeError" in row1["error"]
        assert row2["state"] == "succeeded"

    async def test_loop_exits_promptly_on_shutdown(self, pool):
        """Empty queue + shutdown.set() should exit within one poll interval."""
        clear()
        shutdown = asyncio.Event()
        deps = Deps(pool=pool, steam_client=None)

        async def stopper():
            await asyncio.sleep(0.05)
            shutdown.set()

        # poll_interval=2.0 means without proper wait-on-event the loop
        # would sleep for 2s on empty queue. Asserting it exits within
        # 0.5s proves the asyncio.wait_for(shutdown.wait(),...) pattern.
        await asyncio.wait_for(
            asyncio.gather(
                worker_loop(deps, shutdown=shutdown, poll_interval_sec=2.0),
                stopper(),
            ),
            timeout=0.5,
        )

    async def test_handler_succeeded_after_normal_completion(self, pool):
        clear()

        async def noop(row, deps):
            return

        register("library_sync", noop)
        await _queue_job(pool)

        shutdown = asyncio.Event()
        deps = Deps(pool=pool, steam_client=None)

        async def stopper():
            for _ in range(100):
                row = await pool.read_one("SELECT state FROM jobs WHERE id=1")
                if row and row["state"] in ("succeeded", "failed"):
                    break
                await asyncio.sleep(0.01)
            shutdown.set()

        await asyncio.gather(
            worker_loop(deps, shutdown=shutdown, poll_interval_sec=0.02),
            stopper(),
        )
        row = await pool.read_one("SELECT state, finished_at FROM jobs WHERE id=1")
        assert row["state"] == "succeeded"
        assert row["finished_at"] is not None


class TestBuiltinRegistration:
    async def test_library_sync_handler_is_registered_at_import(self):
        # The autouse fixture restores the snapshot, which was taken
        # after handlers/__init__._register_builtin_handlers ran.
        assert "library_sync" in HANDLERS
