"""New - Not Cached: re-measure recently bought games that are not cached yet.

Order: refresh the Steam library (so a game bought minutes ago is in the table),
measure every game in the check set sequentially, then — on a scheduler run only —
re-measure the games already overdue as "not cached", then push the overdue
monitor. The re-measure is what lets an overdue alert clear within one cron cycle
of the game being fixed (ruling 16).
Cache truth is written only by ``record_measurement`` via ``validate_one_game``;
this handler never writes ``games.status``. The job always succeeds with a summary
unless the circuit breaker trips, which is a global stop and fails the job (the
overdue monitor is still reported first).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.clients import heartbeat
from orchestrator.core.settings import Settings, get_settings
from orchestrator.jobs.handlers.library_sync import _steam_library_sync
from orchestrator.jobs.handlers.validate import validate_one_game
from orchestrator.jobs.measurement import CircuitBreakerTripped, record_measurement
from orchestrator.jobs.new_games import (
    OverdueGame,
    overdue_message,
    select_check_set,
    select_overdue,
)
from orchestrator.jobs.summary import JobSummary

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

# The overdue monitor answers "is a new game stuck uncached?", so only runs the
# scheduler or the T1 SteamPrefill poller started may push it; an API- or
# CLI-sourced run must not reset it.
_OVERDUE_PUSH_SOURCES = ("scheduler", "trigger")


async def _current_source(job: dict[str, Any], deps: Deps) -> str:
    # The cron may promote this job to source='scheduler' after it was claimed
    # (I1); gate on the value the DB holds now, as the worker's heartbeat does.
    from orchestrator.jobs.worker import current_job_source  # runtime: avoids a cycle

    return await current_job_source(deps.pool, job)


async def _measure(game_ids: list[int], job_id: Any, deps: Deps, settings: Settings) -> int:
    """Measure each game sequentially; return how many came back cached.

    One bad game never aborts the batch. ``CircuitBreakerTripped`` propagates.
    """
    cached = 0
    for game_id in game_ids:
        try:
            result = await validate_one_game(deps.pool, deps, game_id, settings)
        except CircuitBreakerTripped:
            raise
        except Exception as e:  # isolate — one bad game never aborts the check
            # A RETURNED 'error' outcome is stamped inside validate_one_game; only
            # the raised path needs the attempt recorded here.
            try:
                await record_measurement(deps.pool, game_id, "error")
            except Exception as stamp_err:  # must not mask the failure that got us here
                _log.warning(
                    "new_games.attempt_record_failed",
                    job_id=job_id,
                    game_id=game_id,
                    error=type(stamp_err).__name__,
                    reason=str(stamp_err)[:200],
                )
            _log.warning(
                "new_games.game_error",
                job_id=job_id,
                game_id=game_id,
                error=type(e).__name__,
                reason=str(e)[:200],
            )
            continue
        if result.outcome == "cached":
            cached += 1
    return cached


async def _report_overdue(job: dict[str, Any], deps: Deps, settings: Settings) -> list[OverdueGame]:
    overdue = await select_overdue(
        deps.pool,
        window_days=settings.new_game_window_days,
        unknown_hours=settings.new_game_unknown_alert_hours,
    )
    if await _current_source(job, deps) in _OVERDUE_PUSH_SOURCES:
        await heartbeat.push(
            settings.kuma_push_new_games_overdue,
            status="down" if overdue else "up",
            msg=overdue_message(overdue),
        )
    for g in overdue:
        _log.info(
            "new_games.overdue",
            job_id=job.get("id"),
            game_id=g.game_id,
            title=g.title,
            reason=g.reason,
        )
    return overdue


async def new_games_check_handler(job: dict[str, Any], deps: Deps) -> JobSummary:
    settings = get_settings()
    job_id = job.get("id")

    try:
        await _steam_library_sync(job, deps)
    except Exception as e:  # a failed sync must not stop the check of known games
        _log.warning(
            "new_games.steam_sync_failed",
            job_id=job_id,
            error=type(e).__name__,
            reason=str(e)[:200],
        )

    game_ids = await select_check_set(deps.pool, window_days=settings.new_game_window_days)
    try:
        cached = await _measure(game_ids, job_id, deps, settings)
        if await _current_source(job, deps) == "scheduler":
            checked = set(game_ids)
            stuck = [
                g.game_id
                for g in await select_overdue(
                    deps.pool,
                    window_days=settings.new_game_window_days,
                    unknown_hours=settings.new_game_unknown_alert_hours,
                )
                if g.reason == "not cached" and g.game_id not in checked
            ]
            await _measure(stuck, job_id, deps, settings)
    except CircuitBreakerTripped:
        # The breaker is a global stop and fails the job, but the overdue monitor
        # must still report; a failure here must not mask the trip.
        try:
            await _report_overdue(job, deps, settings)
        except Exception as e:
            _log.warning(
                "new_games.overdue_report_failed",
                job_id=job_id,
                error=type(e).__name__,
                reason=str(e)[:200],
            )
        raise

    overdue = await _report_overdue(job, deps, settings)
    return JobSummary(
        ok=True,
        msg=f"checked {len(game_ids)} new, {cached} cached; overdue {len(overdue)}",
    )
