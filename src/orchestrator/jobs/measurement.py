"""The ONLY module permitted to write games.status / games.status_measured_at.

Cache truth is what a measurement found on disk. Job outcome is how a job ended.
Before the 2026-09-04 design they shared one column, and on 2026-09-01 an
interrupted prefill batch stamped 1769 games with the dead job's outcome — truth
the next scheduled prefill would have acted on. Everything that wants to say
"this job went badly" calls :func:`record_job_outcome`; only a real cache
measurement reaches :func:`record_measurement`.

A source-scanning test fails the build if any other module UPDATEs the truth
columns.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.clients import heartbeat
from orchestrator.core.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from orchestrator.db.pool import Pool, WriteTx

    # Shared shape of Pool.execute_write and WriteTx.execute.
    _Write = Callable[[str, Sequence[Any] | Mapping[str, Any]], Awaitable[int]]
    # Shared shape of Pool.read_one and WriteTx.read_one.
    _Read = Callable[[str, Sequence[Any] | Mapping[str, Any]], Awaitable[dict[str, Any] | None]]

_log = structlog.get_logger(__name__)

# Truncation for operator-facing strings persisted or logged from here.
_ERROR_TRUNCATE = 200

# When the open breaker was last announced (monotonic seconds), or None.
_last_breaker_notice_at: float | None = None

# outcome -> games.status. 'error' is absent by design: an infrastructure failure
# is not a measurement and must never overwrite cache truth. 'missing' and
# 'partial' are deliberately distinct — nothing on disk is not a failed
# validation, and the two are ranked differently downstream.
_STATUS_FOR: dict[str, str] = {
    "cached": "up_to_date",
    "partial": "validation_failed",
    "missing": "not_downloaded",
}

# How much cache a status claims to have. Only these four are cache truth; a move
# to a lower rank is a loss and is what the circuit breaker counts. Every other
# games.status value (pending_update / downloading / blocked / failed) is job
# lifecycle, carries no rank, and takes no part in the comparison.
_RANK: dict[str, int] = {
    "up_to_date": 3,
    "validation_failed": 2,
    "not_downloaded": 1,
    "unknown": 0,
}


class CircuitBreakerTripped(RuntimeError):  # noqa: N818 — named for the event in the design doc
    """Too many games lost cache state too fast. Writing has stopped."""


def _writer(pool: Pool, tx: WriteTx | None) -> _Write:
    """Pick the write path once per call.

    With ``tx`` the statement joins the caller's open transaction, so a status
    flip and a related row land atomically. Without it, each statement is its own
    committed write. Reads mirror this exactly (see :func:`_reader`).
    """
    return pool.execute_write if tx is None else tx.execute


def _reader(pool: Pool, tx: WriteTx | None) -> _Read:
    """Pick the read path to match :func:`_writer`.

    The breaker's count MUST come from the same connection as its writes. Reading
    through the pool while a caller's transaction is open would count a snapshot
    taken before that caller's own transitions — so a batch inside one
    transaction could never trip the breaker no matter how far it ran.
    """
    return pool.read_one if tx is None else tx.read_one


def _is_downward(prior: str | None, new: str) -> bool:
    """True only for a drop between two ranked truth states.

    pending_update / downloading / blocked / failed carry no rank -- they are not
    cache truth, so transitions involving them are ignored entirely. So is a
    missing row: with no prior there is nothing to have lost.
    """
    if prior is None:
        return False
    p, n = _RANK.get(prior), _RANK.get(new)
    if p is None or n is None:
        return False
    return n < p


def _breaker_notice_due(window_minutes: int) -> bool:
    """True at most once per window; stamps the clock when it says yes.

    A refused write records no transition row, so the count stays frozen at the
    threshold and EVERY later downward measurement recomputes the same trip. The
    only production caller keeps going after the exception (the sweep catches it
    per game), so without this an incident-scale sweep would emit one ERROR line
    and one 10-second-timeout Kuma GET per remaining game — on the order of a
    thousand, and ~20 minutes of pure timeout if Kuma is unreachable.

    Time-boxed rather than latched: a breaker still open an hour later is still
    news, and Kuma needs a fresh DOWN to stay red.
    """
    global _last_breaker_notice_at
    now = time.monotonic()
    if _last_breaker_notice_at is not None and now - _last_breaker_notice_at < window_minutes * 60:
        return False
    _last_breaker_notice_at = now
    return True


def reset_breaker_notice() -> None:
    """Forget that a trip was announced, so the next one notifies. Tests only."""
    global _last_breaker_notice_at
    _last_breaker_notice_at = None


async def _notify_breaker(count: int) -> None:
    """Best-effort push to Uptime Kuma. Never raises.

    Delivery (email, Discord, whatever the operator wired up) is Kuma's job, so
    this is one push and no notification logic of its own. A dead monitor must
    not suppress the exception that actually halts writing — hence the guard
    around a helper that already swallows its own failures.
    """
    settings = get_settings()
    url = settings.kuma_push_measurement_breaker
    if not url:
        return
    try:
        await heartbeat.push(
            url,
            status="down",
            msg=(
                f"{count} games lost cache state within "
                f"{settings.measurement_breaker_window_minutes} minutes"
            ),
        )
    except Exception as exc:
        _log.warning("measurement.breaker_notify_failed", reason=str(exc)[:_ERROR_TRUNCATE])


async def record_measurement(
    pool: Pool, game_id: int, outcome: str, *, tx: WriteTx | None = None, commanded: bool = False
) -> None:
    """Record the result of a cache measurement, or a commanded cache change.

    A real result (``cached`` / ``partial`` / ``missing``) writes cache truth and
    both timestamps. Any other outcome — ``error``, or anything unrecognised —
    writes ONLY ``last_measure_attempt_at``, so the game rotates to the back of
    the measurement queue without its status being touched.

    Every truth write is also logged to ``measurement_transitions``, and a write
    that would drop this game's rank is refused once too many others have dropped
    inside the window (see :class:`CircuitBreakerTripped`). The breaker is
    checked BEFORE any write: a tripped breaker persists nothing at all, so the
    library keeps the last state a trustworthy measurement gave it.

    ``commanded`` is the exception, and it exists because a purge had already
    unlinked the files by the time it called this (security audit SEV-2): a
    refusal there did not preserve truth, it destroyed the only record of a
    change that really happened, leaving a green badge over an empty cache that
    the halted sweep could not correct. An operator purge is KNOWN cache truth,
    not an observation whose trustworthiness is in question — the breaker exists
    to catch an agent lying about what it read, and must not veto a write about
    files this system itself deleted. So a commanded change skips the breaker and
    writes no transition row: the alarm counts unexplained mass loss, and the
    purge's own ``validation_history`` row (written by the caller in the same
    transaction) is its audit trail.

    Args:
        pool: DB pool. Used directly unless ``tx`` is given.
        game_id: games.id to record against.
        outcome: one of ``cached``, ``partial``, ``missing``, ``error``.
        tx: an already-open write transaction to write inside, if the caller has
            one. The write then commits (or rolls back) with the caller's.
        commanded: this system caused the change and knows it happened (a purge),
            rather than having observed it. Skips the breaker and the transition
            log. No effect on the ``error`` path — an infrastructure failure is
            not a measurement whatever commanded it.

    Raises:
        CircuitBreakerTripped: too many games lost cache state inside the window.
            Nothing was written for this game. Never raised when ``commanded``.
    """
    write = _writer(pool, tx)
    new_status = _STATUS_FOR.get(outcome)

    if new_status is None:
        await write(
            "UPDATE games SET last_measure_attempt_at=CURRENT_TIMESTAMP WHERE id=?",
            (game_id,),
        )
        # 'error' is the expected non-measurement and stays quiet. Anything else
        # is a caller bug — a typo like 'cahced' silently stops writing truth for
        # every game it touches, so it has to be loud.
        log = _log.info if outcome == "error" else _log.warning
        log("measurement.attempt_only", game_id=game_id, outcome=outcome)
        return

    read = _reader(pool, tx)
    prior_row = await read("SELECT status FROM games WHERE id=?", (game_id,))
    prior = str(prior_row["status"]) if prior_row else None
    downward = _is_downward(prior, new_status)

    if downward and not commanded:
        settings = get_settings()
        window = settings.measurement_breaker_window_minutes
        recent = await read(
            "SELECT COUNT(*) AS n FROM measurement_transitions "
            "WHERE downward = 1 "
            "  AND occurred_at >= datetime('now', ?)",
            (f"-{window} minutes",),
        )
        in_window = (int(recent["n"]) if recent else 0) + 1
        if in_window >= settings.measurement_breaker_threshold:
            if _breaker_notice_due(window):
                _log.error(
                    "measurement.breaker_tripped",
                    game_id=game_id,
                    prior=prior,
                    new_status=new_status,
                    downward_in_window=in_window,
                    threshold=settings.measurement_breaker_threshold,
                )
                await _notify_breaker(in_window)
            else:
                # Already announced. Still refuse the write — just quietly, so
                # the scale stays recoverable from the log without a thousand
                # ERROR lines and a thousand pushes.
                _log.info(
                    "measurement.breaker_refused",
                    game_id=game_id,
                    prior=prior,
                    new_status=new_status,
                    downward_in_window=in_window,
                )
            raise CircuitBreakerTripped(
                f"{in_window} games lost cache state within {window} minutes; writing halted"
            )

    # F8: measurement does NOT write cached_version — prefill is the sole writer
    # (it controls manifest freshness). A standalone sweep can measure against a
    # stale stored manifest, so stamping current_version here could falsely mark
    # a patched game as cached. See the F8 spec "prefill-sole-writer".
    await write(
        "UPDATE games SET status=?, status_measured_at=CURRENT_TIMESTAMP, "
        "last_measure_attempt_at=CURRENT_TIMESTAMP, last_validated_at=CURRENT_TIMESTAMP "
        "WHERE id=?",
        (new_status, game_id),
    )
    if commanded:
        # No transition row: the log counts unexplained loss, and a batch of
        # deliberate purges arming the alarm against the next sweep would be a
        # false positive. This line is the operator trail in its place.
        _log.info("measurement.commanded", game_id=game_id, prior=prior, new_status=new_status)
        return
    # Durable, because the breaker must survive a restart — a restart is exactly
    # the scenario that produced the 2026-09-01 corruption.
    await write(
        "INSERT INTO measurement_transitions (game_id, prior, new_status, downward) "
        "VALUES (?, ?, ?, ?)",
        (game_id, prior or "unknown", new_status, 1 if downward else 0),
    )
    _log.info(
        "measurement.recorded",
        game_id=game_id,
        outcome=outcome,
        status=new_status,
        prior=prior,
        downward=downward,
    )


async def record_job_outcome(
    pool: Pool, game_id: int, outcome: str, *, tx: WriteTx | None = None
) -> None:
    """Record how a job ended. Never touches cache truth.

    The text is also mirrored into the legacy ``last_error`` column until that
    column is removed: the games API, the CLI and Game_shelf all still read it,
    and the writers that used to maintain it were the same ``status='failed'``
    statements this design deleted. Mirroring keeps those surfaces accurate
    without giving anything but this module a reason to UPDATE ``games``.

    Args:
        pool: DB pool. Used directly unless ``tx`` is given.
        game_id: games.id to record against.
        outcome: operator-facing description of how the job ended. Truncated to
            ``_ERROR_TRUNCATE`` chars, like every other error string persisted
            here.
        tx: an already-open write transaction to write inside, if the caller has
            one.
    """
    write = _writer(pool, tx)
    text = outcome[:_ERROR_TRUNCATE]
    await write(
        "UPDATE games SET last_job_outcome=?, last_job_outcome_at=CURRENT_TIMESTAMP, "
        "last_error=? WHERE id=?",
        (text, text, game_id),
    )
    _log.info("job_outcome.recorded", game_id=game_id, outcome=text)
