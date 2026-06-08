"""F13 — scheduled validation sweep handler.

Re-runs F7 disk-stat validation across the cached Steam library (status
up_to_date + validation_failed) in batches, to catch LRU eviction drift and
recovery. Pre-flight-skips on validator-unhealthy; per-game errors are isolated.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.jobs.handlers.validate import validate_one_game
from orchestrator.validator.self_test import validator_self_test

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE platform='steam' AND status IN ('up_to_date','validation_failed') "
    "ORDER BY id"
)


async def sweep_handler(job: dict[str, Any], deps: Deps) -> None:
    """Validate every cached, non-blocked Steam game in batches (F13).

    Best-effort: an unhealthy validator or a missing steam client is a SKIP (the
    job succeeds — nothing to do), and a per-game failure never aborts the sweep.
    """
    job_id = job.get("id")
    settings = get_settings()

    if deps.steam_client is None:
        _log.info("sweep.skipped", job_id=job_id, reason="no_steam_client")
        return
    if not await validator_self_test(settings):
        _log.info("sweep.skipped", job_id=job_id, reason="validator_unhealthy")
        return

    rows = await deps.pool.read_all(_CANDIDATE_SQL)
    _log.info("sweep.started", job_id=job_id, candidates=len(rows))

    sem = asyncio.Semaphore(settings.sweep_batch_size)
    counts = {"cached": 0, "partial": 0, "missing": 0, "error": 0}
    errors = 0
    evicted = 0
    recovered = 0
    lock = asyncio.Lock()

    async def _one(game_id: int, prior: str) -> None:
        nonlocal errors, evicted, recovered
        async with sem:
            try:
                result = await validate_one_game(deps.pool, deps, game_id, settings)
            except Exception as e:  # isolate — one bad game never aborts the sweep
                async with lock:
                    errors += 1
                _log.warning(
                    "sweep.game_error",
                    job_id=job_id,
                    game_id=game_id,
                    error=type(e).__name__,
                    reason=str(e)[:200],
                )
                return
            async with lock:
                counts[result.outcome] = counts.get(result.outcome, 0) + 1
                # Only a genuine cache-state regression (partial/missing -> the
                # game becomes validation_failed) is an eviction. An 'error'
                # outcome (infra/data failure) leaves the status unchanged and
                # must NOT inflate the drift metric (adversarial finding 1).
                if prior == "up_to_date" and result.outcome in ("partial", "missing"):
                    evicted += 1
                elif prior == "validation_failed" and result.outcome == "cached":
                    recovered += 1

    await asyncio.gather(*(_one(int(r["id"]), str(r["status"])) for r in rows))

    _log.info(
        "sweep.completed",
        job_id=job_id,
        total=len(rows),
        cached=counts["cached"],
        validation_failed=counts["partial"] + counts["missing"],
        validation_error=counts["error"],
        evicted=evicted,
        recovered=recovered,
        errors=errors,
    )
