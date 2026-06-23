"""Ephemeral in-memory job registry for the agent's async operations.

Durability lives in the orchestrator's DB job that drives a call; an agent
restart simply loses in-flight jobs (the orchestrator job retries). State is
not persisted by design. Single-event-loop access; the dict ops are atomic
within a coroutine step so no lock is needed for these simple mutations.

Bounded retention (MEM-1): the agent is a long-lived process, so the store
trims the OLDEST terminal (done/failed) jobs once it exceeds ``max_jobs``.
Running jobs are never evicted — the control plane may still be polling them —
and oldest-terminal-first eviction means a just-finished job survives long
enough to be read before it can be reclaimed.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

_DEFAULT_MAX_JOBS = 1024
_TERMINAL = ("done", "failed")


@dataclass
class _Job:
    state: str = "running"  # running | done | failed
    done: int = 0
    total: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


class AgentJobStore:
    def __init__(self, *, max_jobs: int = _DEFAULT_MAX_JOBS) -> None:
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._max_jobs = max_jobs

    def create(self) -> str:
        job_id = uuid.uuid4().hex
        self._jobs[job_id] = _Job()
        self._evict_terminal()
        return job_id

    def _evict_terminal(self) -> None:
        """Trim oldest terminal jobs until at/under the cap. Never evicts a
        running job; if everything over the cap is still running, the store is
        allowed to exceed the cap rather than drop in-flight work."""
        while len(self._jobs) > self._max_jobs:
            victim = next((jid for jid, job in self._jobs.items() if job.state in _TERMINAL), None)
            if victim is None:
                break  # all over-cap jobs are running — keep them
            del self._jobs[victim]

    def size(self) -> int:
        return len(self._jobs)

    def set_progress(self, job_id: str, done: int, total: int) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.done = done
            job.total = total

    def set_done(self, job_id: str, result: dict[str, Any]) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.state = "done"
            job.result = result

    def set_failed(self, job_id: str, error: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.state = "failed"
            job.error = error

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {
            "state": job.state,
            "done": job.done,
            "total": job.total,
            "result": job.result,
            "error": job.error,
        }
