"""Generic asyncio jobs dispatcher (BL11).

Single-loop topology (spec D10). Atomic claim via SELECT-then-UPDATE
inside `write_transaction()` so concurrent workers (if ever spawned)
don't claim the same job. The loop catches every handler exception so
one bad job can't bring the loop down.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.jobs.handlers import HANDLERS

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool
    from orchestrator.platform.epic.client import EpicClient
    from orchestrator.platform.steam.client import SteamWorkerClient

_log = structlog.get_logger(__name__)

JOB_ERROR_TRUNCATE = 200


@dataclass(frozen=True, slots=True)
class Deps:
    """Handler dependency bundle. Tests construct minimal Deps; production
    builds one in the FastAPI lifespan that carries the singleton
    SteamWorkerClient.
    """

    pool: Pool
    steam_client: SteamWorkerClient | None
    epic_client: EpicClient | None = None


async def claim_next_job(pool: Pool) -> dict[str, Any] | None:
    """Atomically claim the oldest queued job. Returns the row dict
    (kind, id, game_id, platform, payload, state, started_at) with
    `state='running'` and `started_at` set, or None if nothing queued.

    Uses SELECT-then-UPDATE under BEGIN IMMEDIATE; concurrent claims on
    the same pool serialize on the write transaction.
    """
    async with pool.write_transaction() as tx:
        row = await tx.read_one("SELECT id FROM jobs WHERE state='queued' ORDER BY id LIMIT 1")
        if row is None:
            return None
        await tx.execute(
            "UPDATE jobs SET state='running', started_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND state='queued'",
            (row["id"],),
        )
        return await tx.read_one(
            "SELECT id, kind, game_id, platform, state, started_at, payload FROM jobs WHERE id=?",
            (row["id"],),
        )


async def mark_succeeded(pool: Pool, job_id: int) -> None:
    await pool.execute_write(
        "UPDATE jobs SET state='succeeded', finished_at=CURRENT_TIMESTAMP, error=NULL "
        "WHERE id=? AND state='running'",
        (job_id,),
    )


async def mark_failed(pool: Pool, job_id: int, error: str) -> None:
    truncated = error[:JOB_ERROR_TRUNCATE]
    await pool.execute_write(
        "UPDATE jobs SET state='failed', finished_at=CURRENT_TIMESTAMP, error=? "
        "WHERE id=? AND state='running'",
        (truncated, job_id),
    )


async def worker_loop(deps: Deps, *, shutdown: asyncio.Event, poll_interval_sec: float) -> None:
    """Single-loop dispatcher. Runs until `shutdown` is set.

    - Unknown `kind` → job marked failed; loop continues.
    - Handler exception → job marked failed; loop continues.
    - `claim_next_job` failure (DB outage) → log + back off + retry.
    """
    _log.info("jobs.worker.started", poll_interval=poll_interval_sec)
    while not shutdown.is_set():
        try:
            row = await claim_next_job(deps.pool)
        except Exception as e:
            _log.error("jobs.worker.claim_failed", reason=str(e)[:JOB_ERROR_TRUNCATE])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval_sec)
            continue

        if row is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval_sec)
            continue

        job_id = int(row["id"])
        kind = str(row["kind"])
        _log.info("jobs.worker.claimed_job", job_id=job_id, kind=kind)

        handler = HANDLERS.get(kind)
        if handler is None:
            await mark_failed(deps.pool, job_id, f"no handler for kind {kind!r}")
            _log.warning("jobs.handler.no_handler", kind=kind, job_id=job_id)
            continue

        t0 = time.monotonic()
        _log.info("jobs.handler.started", kind=kind, job_id=job_id)
        try:
            await handler(row, deps)
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[: JOB_ERROR_TRUNCATE - 50]}"
            try:
                await mark_failed(deps.pool, job_id, err)
            except Exception as mark_e:
                _log.error(
                    "jobs.handler.mark_failed_failed",
                    job_id=job_id,
                    original_error=err,
                    reason=str(mark_e)[:JOB_ERROR_TRUNCATE],
                )
            _log.warning(
                "jobs.handler.failed",
                kind=kind,
                job_id=job_id,
                kind_error=type(e).__name__,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
            continue

        try:
            await mark_succeeded(deps.pool, job_id)
        except Exception as e:
            _log.error(
                "jobs.handler.mark_succeeded_failed",
                job_id=job_id,
                reason=str(e)[:JOB_ERROR_TRUNCATE],
            )
            continue
        _log.info(
            "jobs.handler.completed",
            kind=kind,
            job_id=job_id,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    _log.info("jobs.worker.stopped")
