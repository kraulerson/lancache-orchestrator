"""The validation sweep's pass boundary (#311).

A full pass over the library costs ~12.5 h against a 6 h ``job_max_runtime_sec``,
so a pass necessarily spans several runs. This module owns the one row that says
which pass is in flight and when it began; ``sweep_handler`` gates its candidates
on it, and completes it when the candidate set is empty.

Kept out of the handler because it is the only durable state the sweep has, and
because "what is a pass" is worth being able to read in one screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SweepPass:
    """The sweep pass currently in flight.

    Attributes:
        number: monotonically increasing pass counter, starting at 1.
        started_at: the SQLite ``CURRENT_TIMESTAMP`` text at which this pass
            began. A game is a candidate for this pass while its
            ``last_measure_attempt_at`` is NULL or at-or-before this stamp.
    """

    number: int
    started_at: str


async def read_pass(pool: Pool) -> SweepPass:
    """The pass in flight. Migration 0017 seeds the row, so this always finds one."""
    row = await pool.read_one("SELECT pass_number, pass_started_at FROM sweep_pass WHERE id=1")
    if row is None:  # pragma: no cover - 0017 seeds it; a missing row is a broken DB
        raise RuntimeError("sweep_pass row is missing — migration 0017 did not run")
    return SweepPass(number=int(row["pass_number"]), started_at=str(row["pass_started_at"]))


async def complete_pass(pool: Pool, current: SweepPass) -> SweepPass:
    """Close ``current`` and open the next pass, starting now.

    The UPDATE is guarded on ``pass_number = current.number`` so a caller working
    from a stale view cannot advance a pass someone else already advanced. That
    matters more than a skipped counter would: a second advance would restamp
    ``pass_started_at``, silently excusing every game measured in between from
    the new pass.

    Returns the pass now in flight — the new one, or the one that was already
    there if this call was the stale loser.
    """
    await pool.execute_write(
        "UPDATE sweep_pass SET pass_number = pass_number + 1, "
        "pass_started_at = CURRENT_TIMESTAMP WHERE id = 1 AND pass_number = ?",
        (current.number,),
    )
    nxt = await read_pass(pool)
    if nxt.number == current.number:  # pragma: no cover - defensive
        _log.warning("sweep.pass_advance_noop", pass_number=current.number)
    return nxt
