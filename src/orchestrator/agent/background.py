"""Tracking for the agent's fire-and-forget background tasks.

Every agent task (prefill, pull, manifest fetch, archive sync) was wired as::

    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)

`discard` releases the strong reference but never inspects the task, and asyncio
only reports an unretrieved exception when the task object is garbage-collected —
which, for a task held in a set until it finishes, means the traceback is
discarded along with the reference. A task that raised therefore vanished without
a single log line.

That is not a theoretical gap. The manifest archive sync loop silently no-oped
for a week (#281); the pattern here is why nothing surfaced it. `db/pool.py`
already guards its own tasks with `_log_bg_task_exception` — the agent simply
never adopted the same discipline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # asyncio is referenced only in annotations
    import asyncio

_log = structlog.get_logger(__name__)


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """Surface an unhandled exception from a fire-and-forget agent task.

    Cancellation is excluded deliberately: the lifespan cancels every tracked
    task on shutdown/redeploy, so reporting that as an error would make each
    normal restart look like a crash and train the operator to ignore exactly
    the signal this exists to provide.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error(
            "agent.background_task_failed",
            task_name=task.get_name(),
            error=str(exc),
            error_type=type(exc).__name__,
        )


def track_background_task(task: asyncio.Task[Any], bg_tasks: set[asyncio.Task[Any]]) -> None:
    """Hold a strong reference to ``task`` until it finishes, and log if it dies.

    The strong reference is required: asyncio only keeps a weak reference to a
    running task, so an untracked fire-and-forget task can be garbage-collected
    mid-flight. Both concerns belong together — the whole class of bug this fixes
    came from doing the first and forgetting the second.
    """
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    task.add_done_callback(_log_task_exception)
