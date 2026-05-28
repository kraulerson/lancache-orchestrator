"""Integration test for ID6: the reaper runs in FastAPI lifespan startup
and cleans up orphaned `running` jobs before the worker spawns."""

from __future__ import annotations

import aiosqlite
import pytest

from orchestrator.jobs.reaper import REAPER_ERROR_MESSAGE

pytestmark = pytest.mark.asyncio


async def _seed_running_job(db_path: str) -> int:
    """Insert a `state='running'` row directly via aiosqlite — bypasses
    the orchestrator pool because the test runs before/around lifespan
    startup."""
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "INSERT INTO jobs (kind, platform, state, source, started_at) "
            "VALUES ('library_sync', 'steam', 'running', 'api', "
            "'2026-05-27 09:00:00')"
        )
        await conn.commit()
        async with conn.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
            return row[0]


async def _read_job_state(db_path: str, job_id: int) -> tuple[str, str | None]:
    async with (
        aiosqlite.connect(db_path) as conn,
        conn.execute("SELECT state, error FROM jobs WHERE id=?", (job_id,)) as cur,
    ):
        row = await cur.fetchone()
        return row[0], row[1]


class TestLifespanReaperIntegration:
    async def test_orphan_running_job_is_reaped_at_boot(self, db_path, monkeypatch):
        """Seed a job in state='running' BEFORE lifespan, boot the app,
        verify the row was flipped to 'failed' with the reaper's error
        message."""
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))

        # Pre-seed the orphan BEFORE lifespan opens the pool.
        job_id = await _seed_running_job(str(db_path))

        app = create_app()
        async with LifespanManager(app):
            # Lifespan startup completed; reaper has run.
            state, error = await _read_job_state(str(db_path), job_id)

        assert state == "failed"
        assert error == REAPER_ERROR_MESSAGE

    async def test_non_running_jobs_untouched_by_reaper(self, db_path, monkeypatch):
        """Succeeded/failed/cancelled rows must NOT be modified by the reaper
        — only `running` is orphaned.

        Note: this test intentionally excludes `queued` because the BL11
        jobs worker spawns inside lifespan and would legitimately claim
        a queued row during the test window (then fail it because no
        steam client is authenticated). That's worker behavior, not
        reaper behavior. The reaper's contract is "running → failed";
        queued is the worker's domain.
        """
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))

        # Seed terminal states only (no queued — see docstring).
        async with aiosqlite.connect(str(db_path)) as conn:
            for state in ("succeeded", "failed", "cancelled"):
                await conn.execute(
                    "INSERT INTO jobs (kind, platform, state, source, "
                    "started_at, finished_at) "
                    "VALUES ('library_sync', 'steam', ?, 'api', "
                    "'2026-05-27 09:00:00', '2026-05-27 09:05:00')",
                    (state,),
                )
            await conn.commit()
            async with conn.execute("SELECT id, state FROM jobs ORDER BY id") as cur:
                rows_before = [(r[0], r[1]) for r in await cur.fetchall()]

        app = create_app()
        async with LifespanManager(app):
            pass  # lifespan startup + shutdown both run

        async with (
            aiosqlite.connect(str(db_path)) as conn,
            conn.execute("SELECT id, state FROM jobs ORDER BY id") as cur,
        ):
            rows_after = [(r[0], r[1]) for r in await cur.fetchall()]

        assert rows_before == rows_after

    async def test_empty_jobs_table_boots_cleanly(self, db_path, monkeypatch):
        from asgi_lifespan import LifespanManager

        from orchestrator.api.main import create_app

        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))

        app = create_app()
        async with LifespanManager(app):
            pass  # boot + shutdown should not raise
