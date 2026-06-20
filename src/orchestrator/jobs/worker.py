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

from orchestrator.core.logging import new_correlation_id
from orchestrator.db.pool import PoolError
from orchestrator.jobs.handlers import HANDLERS

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool
    from orchestrator.platform.epic.client import EpicClient
    from orchestrator.platform.steam.client import SteamWorkerClient
    from orchestrator.platform.steam.prefill_driver import SteamPrefillDriver

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
    prefill_driver: SteamPrefillDriver | None = None


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


# A terminal status write that silently fails leaves the job stuck 'running';
# the next-boot reaper then mislabels a job that actually succeeded as 'failed'
# (audit 2026-06-09). Retry transient pool errors a few times to shrink that
# window before giving up.
_MARK_RETRY_ATTEMPTS = 3
_MARK_RETRY_BASE_DELAY_SEC = 0.2


async def _write_job_status_with_retry(pool: Pool, sql: str, params: tuple[Any, ...]) -> None:
    for attempt in range(_MARK_RETRY_ATTEMPTS):
        try:
            await pool.execute_write(sql, params)
            return
        except PoolError:
            if attempt == _MARK_RETRY_ATTEMPTS - 1:
                raise  # exhausted retries — let the caller log it
            await asyncio.sleep(_MARK_RETRY_BASE_DELAY_SEC * (2**attempt))


async def mark_succeeded(pool: Pool, job_id: int) -> None:
    await _write_job_status_with_retry(
        pool,
        "UPDATE jobs SET state='succeeded', finished_at=CURRENT_TIMESTAMP, error=NULL "
        "WHERE id=? AND state='running'",
        (job_id,),
    )


async def mark_failed(pool: Pool, job_id: int, error: str) -> None:
    truncated = error[:JOB_ERROR_TRUNCATE]
    await _write_job_status_with_retry(
        pool,
        "UPDATE jobs SET state='failed', finished_at=CURRENT_TIMESTAMP, error=? "
        "WHERE id=? AND state='running'",
        (truncated, job_id),
    )


async def worker_loop(
    deps: Deps,
    *,
    shutdown: asyncio.Event,
    poll_interval_sec: float,
    job_max_runtime_sec: float = 0.0,
) -> None:
    """Single-loop dispatcher. Runs until `shutdown` is set.

    - Unknown `kind` → job marked failed; loop continues.
    - Handler exception → job marked failed; loop continues.
    - `claim_next_job` failure (DB outage) → log + back off + retry.
    - `job_max_runtime_sec > 0` → each handler is bounded by `asyncio.wait_for`;
      a wedged handler is cancelled and the job marked failed, so it can't hold
      the single worker loop forever (self-heals without a process restart).

    Each job runs inside its own `correlation_id` (+ `job_id`/`job_kind`) bound
    into contextvars, so every log line it emits — worker, handler, validator,
    pool — is greppable by one ID.
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
        # Bind a correlation_id (+ job_id/kind) for the whole job, so every log
        # line it emits is greppable by one ID — the job-side analogue of the
        # HTTP request_context(). bound_contextvars resets all of them on exit,
        # including on `continue`.
        with structlog.contextvars.bound_contextvars(
            correlation_id=new_correlation_id(), job_id=job_id, job_kind=kind
        ):
            _log.info("jobs.worker.claimed_job", job_id=job_id, kind=kind)

            handler = HANDLERS.get(kind)
            if handler is None:
                await mark_failed(deps.pool, job_id, f"no handler for kind {kind!r}")
                _log.warning("jobs.handler.no_handler", kind=kind, job_id=job_id)
                continue

            t0 = time.monotonic()
            _log.info("jobs.handler.started", kind=kind, job_id=job_id)
            try:
                if job_max_runtime_sec > 0:
                    await asyncio.wait_for(handler(row, deps), timeout=job_max_runtime_sec)
                else:
                    await handler(row, deps)
            except Exception as e:
                # A TimeoutError under an active budget means wait_for cancelled a
                # wedged handler — label it distinctly. (TimeoutError is an
                # Exception, so it lands here.)
                timed_out = isinstance(e, TimeoutError) and job_max_runtime_sec > 0
                if timed_out:
                    err = f"job exceeded max runtime of {job_max_runtime_sec}s (cancelled)"
                    event = "jobs.handler.timed_out"
                    # The handler was cancelled mid-flight — CancelledError
                    # bypasses its own 'downloading' -> 'failed' reset. The worker
                    # is NOT cancelled, so reset the game here (UAT-11 F-INT-1).
                    game_id = row.get("game_id")
                    if game_id is not None:
                        with contextlib.suppress(Exception):
                            await deps.pool.execute_write(
                                "UPDATE games SET status='failed', last_error=? "
                                "WHERE id=? AND status='downloading'",
                                (err, game_id),
                            )
                else:
                    err = f"{type(e).__name__}: {str(e)[: JOB_ERROR_TRUNCATE - 50]}"
                    event = "jobs.handler.failed"
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
                    event,
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
