"""A fire-and-forget agent task that dies must say so.

Every agent background task was wired as::

    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)

`discard` drops the reference without ever inspecting the task, and asyncio does
not surface an exception nobody retrieves from a task that is still referenced —
so a task that raised simply vanished. That is not hypothetical: the manifest
archive sync loop no-oped for a week (#281) with nothing in the logs, and this
pattern is why nobody could see it.

`db/pool.py` already got this right (`_log_bg_task_exception`); the agent never
adopted it.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog

from orchestrator.agent.background import track_background_task

pytestmark = pytest.mark.asyncio


async def test_a_failing_background_task_is_logged():
    bg: set[asyncio.Task] = set()

    async def _boom() -> None:
        raise RuntimeError("archive sync exploded")

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(_boom())
        track_background_task(task, bg)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let done-callbacks run

    failures = [m for m in logs if m.get("event") == "agent.background_task_failed"]
    assert failures, "a background task died with no log line — the silent failure"
    assert failures[0]["log_level"] == "error"
    assert "archive sync exploded" in failures[0]["error"]
    assert failures[0]["error_type"] == "RuntimeError"


async def test_a_successful_background_task_logs_nothing():
    bg: set[asyncio.Task] = set()

    async def _fine() -> None:
        return None

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(_fine())
        track_background_task(task, bg)
        await task
        await asyncio.sleep(0)

    assert not [m for m in logs if m.get("event") == "agent.background_task_failed"]


async def test_a_cancelled_background_task_is_not_reported_as_a_failure():
    """Shutdown cancels every tracked task; that is normal, not a fault. Logging
    it as an error would make every redeploy look like a crash and train the
    operator to ignore the very signal this exists to provide."""
    bg: set[asyncio.Task] = set()

    async def _forever() -> None:
        await asyncio.sleep(3600)

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(_forever())
        track_background_task(task, bg)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert not [m for m in logs if m.get("event") == "agent.background_task_failed"]


async def test_the_task_reference_is_still_released():
    """The original reason for the callback — dropping the strong reference so
    the set does not grow forever — must survive."""
    bg: set[asyncio.Task] = set()

    async def _fine() -> None:
        return None

    task = asyncio.create_task(_fine())
    track_background_task(task, bg)
    assert task in bg, "task must be held while in flight, or it can be GC'd mid-run"
    await task
    await asyncio.sleep(0)
    assert task not in bg, "finished task was never released"
