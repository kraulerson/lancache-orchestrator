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

# Feature B (final-review I1): an eviction event's ts marks the START of a burst.
# cache-catcher then suppresses further alerts for its COOLDOWN (900 s, see
# tools/cache_catcher/fanotify_guard.py) while deletes may continue, and a game is
# stamped attempted only after its disk-stat ends. So the forced pass's boundary is
# pushed past the burst: COOLDOWN + EVICT_WINDOW (60 s) + alert/poll latency. The
# cost is that ~20 min of measurements are repeated once per eviction.
EVICTION_BOUNDARY_MARGIN_SEC = 1200


@dataclass(frozen=True, slots=True)
class SweepPass:
    """The sweep pass currently in flight.

    Attributes:
        number: monotonically increasing pass counter, starting at 1.
        started_at: the SQLite ``CURRENT_TIMESTAMP`` text at which this pass
            began. A game is a candidate for this pass while its
            ``last_measure_attempt_at`` is NULL or at-or-before this stamp.
        forced: True while a pass restarted by a cache eviction is in flight
            (migration 0020). The eviction watch keeps a sweep queued until
            ``complete_pass`` clears it.
        last_eviction_ts: the newest eviction event acted on (ISO-8601 UTC, as
            cache-catcher writes it), or None if none ever was. Survives pass
            completion so a restart never acts on the same eviction twice.
    """

    number: int
    started_at: str
    forced: bool = False
    last_eviction_ts: str | None = None


async def read_pass(pool: Pool) -> SweepPass:
    """The pass in flight. Migration 0017 seeds the row, so this always finds one."""
    row = await pool.read_one(
        "SELECT pass_number, pass_started_at, forced, last_eviction_ts FROM sweep_pass WHERE id=1"
    )
    if row is None:  # pragma: no cover - 0017 seeds it; a missing row is a broken DB
        raise RuntimeError("sweep_pass row is missing — migration 0017 did not run")
    ack = row["last_eviction_ts"]
    return SweepPass(
        number=int(row["pass_number"]),
        started_at=str(row["pass_started_at"]),
        forced=bool(row["forced"]),
        last_eviction_ts=str(ack) if ack is not None else None,
    )


async def restart_pass(pool: Pool, *, started_at: str, eviction_ts: str) -> SweepPass:
    """Open a new, forced pass whose boundary is ``started_at`` (an eviction time).

    Every game last attempted at-or-before the eviction becomes a candidate
    again. ``started_at`` arrives in ISO-8601 (``YYYY-MM-DDTHH:MM:SSZ``) and is
    stored in ``CURRENT_TIMESTAMP`` format, because the candidate query compares
    it as text with ``games.last_measure_attempt_at`` and the ISO ``T`` would sort
    after the space.

    Unguarded on purpose: an eviction supersedes whatever pass is in flight. A
    sweep still running on the old pass calls ``complete_pass`` with a stale view,
    and that call's pass-number guard makes it a no-op.
    """
    # Boundary = min(eviction ts, LXC now) + margin, or now + margin when the ts
    # cannot be parsed (strftime gives NULL for e.g. month 13). The min() stops a
    # NAS clock that runs ahead from making a boundary no measurement can pass;
    # the margin covers the rest of the burst (see EVICTION_BOUNDARY_MARGIN_SEC).
    await pool.execute_write(
        "UPDATE sweep_pass SET pass_number = pass_number + 1, "
        "pass_started_at = datetime("
        "  coalesce(min(CAST(strftime('%s', ?) AS INTEGER), CAST(strftime('%s','now') AS INTEGER)),"
        "           CAST(strftime('%s','now') AS INTEGER)) + ?, 'unixepoch'), "
        "forced = 1, last_eviction_ts = ? WHERE id = 1",
        (started_at, EVICTION_BOUNDARY_MARGIN_SEC, eviction_ts),
    )
    nxt = await read_pass(pool)
    _log.info(
        "sweep.pass_restarted",
        pass_number=nxt.number,
        pass_started_at=nxt.started_at,
        eviction_ts=eviction_ts,
    )
    return nxt


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
        "pass_started_at = CURRENT_TIMESTAMP, forced = 0 WHERE id = 1 AND pass_number = ?",
        (current.number,),
    )
    nxt = await read_pass(pool)
    if nxt.number == current.number:  # pragma: no cover - defensive
        _log.warning("sweep.pass_advance_noop", pass_number=current.number)
    return nxt
