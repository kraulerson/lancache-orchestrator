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

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from orchestrator.db.pool import Pool, WriteTx

    # Shared shape of Pool.execute_write and WriteTx.execute.
    _Write = Callable[[str, Sequence[Any] | Mapping[str, Any]], Awaitable[int]]

_log = structlog.get_logger(__name__)

# outcome -> games.status. 'error' is absent by design: an infrastructure failure
# is not a measurement and must never overwrite cache truth. 'missing' and
# 'partial' are deliberately distinct — nothing on disk is not a failed
# validation, and the two are ranked differently downstream.
_STATUS_FOR: dict[str, str] = {
    "cached": "up_to_date",
    "partial": "validation_failed",
    "missing": "not_downloaded",
}


def _writer(pool: Pool, tx: WriteTx | None) -> _Write:
    """Pick the write path once per call.

    With ``tx`` the statement joins the caller's open transaction, so a status
    flip and a related row land atomically. Without it, each statement is its own
    committed write. Reads mirror this exactly (``tx.read_one`` when there is a
    transaction, ``pool.read_one`` otherwise).
    """
    return pool.execute_write if tx is None else tx.execute


async def record_measurement(
    pool: Pool, game_id: int, outcome: str, *, tx: WriteTx | None = None
) -> None:
    """Record the result of a cache measurement.

    A real result (``cached`` / ``partial`` / ``missing``) writes cache truth and
    both timestamps. Any other outcome — ``error``, or anything unrecognised —
    writes ONLY ``last_measure_attempt_at``, so the game rotates to the back of
    the measurement queue without its status being touched.

    Args:
        pool: DB pool. Used directly unless ``tx`` is given.
        game_id: games.id to record against.
        outcome: one of ``cached``, ``partial``, ``missing``, ``error``.
        tx: an already-open write transaction to write inside, if the caller has
            one. The write then commits (or rolls back) with the caller's.
    """
    write = _writer(pool, tx)
    new_status = _STATUS_FOR.get(outcome)

    if new_status is None:
        await write(
            "UPDATE games SET last_measure_attempt_at=CURRENT_TIMESTAMP WHERE id=?",
            (game_id,),
        )
        _log.info("measurement.attempt_only", game_id=game_id, outcome=outcome)
        return

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
    _log.info("measurement.recorded", game_id=game_id, outcome=outcome, status=new_status)


async def record_job_outcome(
    pool: Pool, game_id: int, outcome: str, *, tx: WriteTx | None = None
) -> None:
    """Record how a job ended. Never touches cache truth.

    Args:
        pool: DB pool. Used directly unless ``tx`` is given.
        game_id: games.id to record against.
        outcome: operator-facing description of how the job ended. Truncated to
            200 chars, like every other error string persisted here.
        tx: an already-open write transaction to write inside, if the caller has
            one.
    """
    write = _writer(pool, tx)
    text = outcome[:200]
    await write(
        "UPDATE games SET last_job_outcome=?, last_job_outcome_at=CURRENT_TIMESTAMP WHERE id=?",
        (text, game_id),
    )
    _log.info("job_outcome.recorded", game_id=game_id, outcome=text)
