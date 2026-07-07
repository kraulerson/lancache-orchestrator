"""F13 — scheduled validation sweep handler.

Re-runs F7 disk-stat validation across the cached Steam library (status
up_to_date + validation_failed) in batches, to catch LRU eviction drift and
recovery. Pre-flight-skips on validator-unhealthy; per-game errors are isolated.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.jobs.handlers.validate import validate_one_game
from orchestrator.validator.self_test import validator_self_test

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

# Includes 'unknown' so a newly-purchased game (inserted at the default 'unknown'
# by library_sync, then cached by the host SteamPrefill cron) is auto-validated by
# the scheduled gated sweep — there is no scheduled FULL sweep, so without this an
# 'unknown' game would never be validated. `owned = 1` bounds the churn; an
# uncovered 'unknown' game returns outcome='error' which validate leaves untouched.
_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE status IN ('unknown','up_to_date','validation_failed') AND owned = 1 "
    "ORDER BY id"
)

# `full` mode (validate-all backfill, 2026-06-24): validate EVERY game across
# all platforms, not just the already-cached subset. Carried on jobs.payload
# `{"full": true}`.
_CANDIDATE_SQL_FULL = "SELECT id, status FROM games ORDER BY id"


async def sweep_handler(job: dict[str, Any], deps: Deps) -> None:
    """Validate every cached, non-blocked game (Steam or Epic) in batches (F13).

    Best-effort: an unhealthy validator or a missing agent client is a SKIP (the
    job succeeds — nothing to do), and a per-game failure never aborts the sweep.
    """
    job_id = job.get("id")
    settings = get_settings()

    if deps.agent_client is None:
        _log.info("sweep.skipped", job_id=job_id, reason="no_agent_client")
        return
    # re-arch ④: pass agent_client so that, when agent_enabled, validator health
    # is sourced from the agent (which owns the cache mount) rather than the
    # local path — the control plane on the LXC has no local cache mount.
    if not await validator_self_test(settings, agent_client=deps.agent_client):
        _log.info("sweep.skipped", job_id=job_id, reason="validator_unhealthy")
        return

    try:
        full = bool(json.loads(job.get("payload") or "{}").get("full", False))
    except (json.JSONDecodeError, TypeError, AttributeError):
        full = False
    candidate_sql = _CANDIDATE_SQL_FULL if full else _CANDIDATE_SQL
    rows = await deps.pool.read_all(candidate_sql)
    _log.info("sweep.started", job_id=job_id, candidates=len(rows), full=full)

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
