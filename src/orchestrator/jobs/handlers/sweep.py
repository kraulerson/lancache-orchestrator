"""F13 — scheduled validation sweep handler.

Re-runs F7 disk-stat validation across every owned game (Steam and Epic), least-
recently-attempted first, in batches — there is no status filter: selecting on
status was defect D2 (1357 Steam games at 'not_downloaded' were invisible to
every sweep from 2026-06-18 onward). Ordering by last_measure_attempt_at means
an interrupted sweep resumes by construction on its next run, with no persisted
cursor. Pre-flight-skips on validator-unhealthy.

Three per-game failures, three different answers (Task 6):

* cancellation (the worker's runtime budget expired) — record the ATTEMPT, write
  no cache truth, re-raise so the job actually ends;
* any other exception (including the agent's httpx read timeouts, which surface
  as ``AgentError``) — record the attempt, count the error, carry on;
* ``CircuitBreakerTripped`` — stop the whole sweep, because the breaker means
  writing has halted.
"""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.jobs.handlers.validate import validate_one_game
from orchestrator.jobs.measurement import CircuitBreakerTripped, record_measurement
from orchestrator.jobs.summary import JobSummary
from orchestrator.jobs.sweep_pass import complete_pass, read_pass
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
#
# #311 adds the pass gate. Recency ordering alone gave no BOUNDARY: the handler
# re-queried all 3212 candidates every run, so `sweep.completed` could only fire
# if one 6h run covered a library that takes ~12.5h — it never fired, and the
# Kuma monitor could never go green. Gating on `pass_started_at` turns an empty
# candidate set into a proof of coverage instead of a coincidence.
#
# `<=` rather than `<`: CURRENT_TIMESTAMP has one-second resolution, so a game
# stamped in the same second the pass began would otherwise be excused from the
# whole pass. The cost of `<=` is re-measuring a handful of boundary games; the
# cost of `<` is a game going unmeasured for an extra 12.5h. Wrong in the safe
# direction.
_CANDIDATE_SQL = (
    "SELECT id, status, size_bytes FROM games "
    "WHERE owned = 1 "
    "AND (last_measure_attempt_at IS NULL OR last_measure_attempt_at <= ?) "
    "ORDER BY last_measure_attempt_at ASC NULLS FIRST, id ASC"
)

# `full` mode additionally includes unowned games, and is deliberately NOT pass-
# gated: it is the Game_shelf "validate everything now" button, a manual
# override. A run that ignores the pass cannot stand in as proof of pass
# coverage, so it neither filters on the marker nor advances it.
_CANDIDATE_SQL_FULL = (
    "SELECT id, status, size_bytes FROM games "
    "ORDER BY last_measure_attempt_at ASC NULLS FIRST, id ASC"
)


def _bytes(n: int) -> str:
    """Cache size for a one-line monitor message.

    Progress is reported in bytes because games/hour misled this project twice:
    2455 of 3212 owned games have no manifest and cost nothing to validate,
    while 3.6 TiB of the 8.1 TiB total sits in 39 titles. A run's game count
    says nothing about how much of the library it covered.
    """
    gib = n / 1024**3
    return f"{gib / 1024:.1f} TiB" if gib >= 1024 else f"{gib:.1f} GiB"


async def sweep_handler(job: dict[str, Any], deps: Deps) -> JobSummary | None:
    """Validate every owned game (Steam or Epic), in batches (F13).

    Best-effort: an unhealthy validator or a missing agent client is a SKIP (the
    job succeeds — nothing to do), and a per-game failure never aborts the sweep.

    Returns a JobSummary describing the PASS, not the run (#311): a sweep that
    drains its candidates completes the pass, and one that reaches its deadline
    reports partial progress and succeeds anyway. Only a tripped breaker fails
    the job now, so `failed` on a sweep once again means genuinely broken.
    """
    job_id = job.get("id")
    settings = get_settings()

    if deps.agent_client is None:
        _log.info("sweep.skipped", job_id=job_id, reason="no_agent_client")
        return None
    # re-arch ④: pass agent_client so that, when agent_enabled, validator health
    # is sourced from the agent (which owns the cache mount) rather than the
    # local path — the control plane on the LXC has no local cache mount.
    if not await validator_self_test(settings, agent_client=deps.agent_client):
        _log.info("sweep.skipped", job_id=job_id, reason="validator_unhealthy")
        return None

    try:
        full = bool(json.loads(job.get("payload") or "{}").get("full", False))
    except (json.JSONDecodeError, TypeError, AttributeError):
        full = False

    current = None if full else await read_pass(deps.pool)
    if current is None:
        rows = await deps.pool.read_all(_CANDIDATE_SQL_FULL)
    else:
        rows = await deps.pool.read_all(_CANDIDATE_SQL, (current.started_at,))

    total_bytes = sum(int(r["size_bytes"] or 0) for r in rows)

    # Stop starting new games this long before the worker would cancel us. The
    # cancel was never a fault — it was a 12.5h pass meeting a 6h cap — but it
    # marked the job failed and pushed Kuma DOWN on a sweep that was working
    # perfectly, and it cancelled mid-validate, which is how orphaned attempt
    # writes land after the job is already recorded (#314).
    budget = settings.job_max_runtime_sec
    deadline = monotonic() + budget - settings.sweep_deadline_margin_sec if budget > 0 else None

    _log.info(
        "sweep.started",
        job_id=job_id,
        candidates=len(rows),
        full=full,
        pass_number=current.number if current else None,
        remaining_bytes=total_bytes,
    )

    sem = asyncio.Semaphore(settings.sweep_batch_size)
    counts = {"cached": 0, "partial": 0, "missing": 0, "error": 0}
    errors = 0
    evicted = 0
    recovered = 0
    lock = asyncio.Lock()
    # The breaker that stopped this sweep, once one game has hit it.
    tripped: CircuitBreakerTripped | None = None
    # Set once the deadline stops a game from starting. A pass is complete only
    # if neither this nor the breaker cut the run short.
    deadline_hit = False
    attempted = 0
    attempted_bytes = 0

    async def _record_attempt(game_id: int) -> None:
        """Stamp last_measure_attempt_at and nothing else.

        An outcome of 'error' is not a measurement, so measurement.py writes no
        cache truth for it — the game just rotates to the back of the queue. The
        guard is here because this runs on the failure path, including during
        shutdown: a DB write that fails while the process is going down must not
        mask the failure (or the cancellation) that brought us here.
        """
        try:
            await record_measurement(deps.pool, game_id, "error")
        except Exception as e:
            _log.warning(
                "sweep.attempt_record_failed",
                job_id=job_id,
                game_id=game_id,
                error=type(e).__name__,
                reason=str(e)[:200],
            )

    async def _one(game_id: int, prior: str, size: int) -> None:
        nonlocal errors, evicted, recovered, tripped, deadline_hit, attempted, attempted_bytes
        async with sem:
            # Short-circuit once the breaker has tripped: no validation, and no
            # attempt stamp either — this game was never measured.
            if tripped is not None:
                return
            # Same shape for the deadline: leave the game unstamped so it stays a
            # candidate for this pass and the next run picks it up at the front.
            if deadline is not None and monotonic() >= deadline:
                async with lock:
                    deadline_hit = True
                return
            async with lock:
                attempted += 1
                attempted_bytes += size
            try:
                result = await validate_one_game(deps.pool, deps, game_id, settings)
            except asyncio.CancelledError:
                # The 6h runtime budget expired, or the job was cancelled. Record
                # the ATTEMPT so the game rotates to the back, write no cache
                # truth, then re-raise so the job actually ends. This is the
                # 2026-09-01 replay: a cancelled sweep must corrupt nothing.
                await _record_attempt(game_id)
                raise
            except CircuitBreakerTripped as e:
                # Not a per-game error — the breaker is a global stop. The spec
                # says a tripped breaker STOPS writing, and a sweep that keeps
                # issuing refused writes for hours is not stopped. Abandoning the
                # remaining games costs nothing: they keep their older
                # last_measure_attempt_at, so the next run picks them up first.
                async with lock:
                    already = tripped is not None
                    if not already:
                        tripped = e
                if not already:
                    _log.error(
                        "sweep.breaker_tripped",
                        job_id=job_id,
                        game_id=game_id,
                        reason=str(e)[:200],
                    )
                return
            except Exception as e:  # isolate — one bad game never aborts the sweep
                # Includes the agent's httpx read timeouts (AgentError). A
                # RETURNED 'error' outcome is already stamped inside
                # validate_one_game; only the raised path needs stamping here.
                await _record_attempt(game_id)
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

    await asyncio.gather(
        *(_one(int(r["id"]), str(r["status"]), int(r["size_bytes"] or 0)) for r in rows)
    )

    if tripped is not None:
        # Fail the job with the breaker's own message: an operator reading the
        # jobs table has to see WHY the sweep stopped, not a truncated run that
        # looks like a success.
        _log.error(
            "sweep.aborted",
            job_id=job_id,
            total=len(rows),
            cached=counts["cached"],
            validation_failed=counts["partial"] + counts["missing"],
            validation_error=counts["error"],
            evicted=evicted,
            recovered=recovered,
            errors=errors,
            reason=str(tripped)[:200],
        )
        raise tripped

    outcome = {
        "job_id": job_id,
        "total": len(rows),
        "cached": counts["cached"],
        "validation_failed": counts["partial"] + counts["missing"],
        "validation_error": counts["error"],
        "evicted": evicted,
        "recovered": recovered,
        "errors": errors,
    }

    if full:
        # Not pass-gated, so it proves nothing about pass coverage and advances
        # nothing. It is still a real run worth reporting.
        _log.info("sweep.completed", **outcome, full=True)
        return JobSummary(ok=True, msg=f"full sweep: {attempted} games, {_bytes(attempted_bytes)}")

    # `full` is False here, so read_pass ran and `current` is set; the check is
    # for the type checker, not for a case that can happen.
    if current is None:  # pragma: no cover - unreachable
        return JobSummary(ok=True, msg=f"sweep: {attempted} games, {_bytes(attempted_bytes)}")

    if deadline_hit:
        # The honest middle state: healthy, incomplete, and not claiming
        # otherwise. The job SUCCEEDS and the monitor stays green — Kuma is
        # answering "is the schedule still running?", and it is.
        _log.info(
            "sweep.pass_progressed",
            **outcome,
            pass_number=current.number,
            attempted=attempted,
            attempted_bytes=attempted_bytes,
            remaining_bytes=total_bytes - attempted_bytes,
        )
        return JobSummary(
            ok=True,
            msg=(
                f"pass {current.number} partial: {attempted}/{len(rows)} games, "
                f"{_bytes(attempted_bytes)} of {_bytes(total_bytes)}"
            ),
        )

    # Nothing cut the run short, so every candidate was attempted and the pass is
    # provably covered end to end — the first time this system can say that.
    nxt = await complete_pass(deps.pool, current)
    _log.info(
        "sweep.pass_completed",
        **outcome,
        pass_number=current.number,
        attempted=attempted,
        attempted_bytes=attempted_bytes,
        next_pass=nxt.number,
    )
    _log.info("sweep.completed", **outcome)
    return JobSummary(
        ok=True,
        msg=f"pass {current.number} complete: {attempted} games, {_bytes(attempted_bytes)}",
    )
