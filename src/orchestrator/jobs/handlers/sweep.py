"""F13 — scheduled validation sweep handler.

Re-runs F7 disk-stat validation across every owned game (Steam and Epic), least-
recently-attempted first, in batches — there is no status filter: selecting on
status was defect D2 (1357 Steam games at 'not_downloaded' were invisible to
every sweep from 2026-06-18 onward). Ordering by last_measure_attempt_at means
an interrupted sweep resumes by construction on its next run, with no persisted
cursor. Pre-flight-skips on validator-unhealthy; per-game errors are isolated.
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

# Gated sweep: every owned game, least-recently-attempted first.
#
# There is deliberately NO `status IN (...)` filter. The previous filter omitted
# 'not_downloaded', which made 1357 Steam games invisible to every sweep from
# 2026-06-18 onward. Selecting on status at all is the bug class; removing the
# filter removes it permanently rather than adding one more value to a list.
#
# Ordering by last_measure_attempt_at (NULLS FIRST) replaces the old ORDER BY id.
# It needs no persisted cursor: games already attempted sort to the back, so an
# interrupted sweep resumes correctly on its next run by construction.
_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE owned = 1 "
    "ORDER BY last_measure_attempt_at ASC NULLS FIRST, id ASC"
)

# `full` mode additionally includes unowned games.
_CANDIDATE_SQL_FULL = (
    "SELECT id, status FROM games ORDER BY last_measure_attempt_at ASC NULLS FIRST, id ASC"
)


async def sweep_handler(job: dict[str, Any], deps: Deps) -> None:
    """Validate every owned game (Steam or Epic), in batches (F13).

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
                # 'not_downloaded' counts as a recovery too: the sweep now reaches
                # it (no status filter), and measurement.py maps outcome='missing'
                # to status='not_downloaded' — so a game that was truly absent and
                # is now found cached is exactly as much a recovery as one that
                # was merely partial.
                elif (
                    prior in ("validation_failed", "not_downloaded") and result.outcome == "cached"
                ):
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
