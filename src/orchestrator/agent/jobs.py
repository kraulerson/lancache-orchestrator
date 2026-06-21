"""Ephemeral in-memory job registry for the agent's async operations.

Durability lives in the orchestrator's DB job that drives a call; an agent
restart simply loses in-flight jobs (the orchestrator job retries). State is
not persisted by design. Single-event-loop access; the dict ops are atomic
within a coroutine step so no lock is needed for these simple mutations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class _Job:
    state: str = "running"  # running | done | failed
    done: int = 0
    total: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None


class AgentJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}

    def create(self) -> str:
        job_id = uuid.uuid4().hex
        self._jobs[job_id] = _Job()
        return job_id

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
