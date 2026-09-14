"""A commanded measurement is exempt from the veto, but not from the history.

#310. ``commanded=True`` used to do two unrelated jobs behind one flag: skip the
circuit breaker's veto, AND write no ``measurement_transitions`` row at all. The
second half made a bulk purge invisible — the immutable log that exists to answer
"what happened to the library" simply had no entry for the largest deliberate
cache change the system can make.

Splitting them is not free, and the third test here is the reason: the breaker
counts downward rows in a rolling window, so merely writing the purge's rows would
arm the alarm against the *next* sweep's first honest measurement. The row is
therefore written AND marked, and the breaker's count excludes marked rows.

What must stay true (it is why the flag exists at all — security audit SEV-2): the
files a purge deleted are already gone, so a veto there does not preserve truth, it
discards the only record of a change that really happened and leaves a green badge
over an empty cache.
"""

from __future__ import annotations

import pytest

from orchestrator.core.settings import get_settings
from orchestrator.jobs.measurement import CircuitBreakerTripped, record_measurement

pytestmark = pytest.mark.asyncio


async def _seed_games(pool, n: int, *, status: str = "up_to_date", prefix: str = "c") -> list[int]:
    ids: list[int] = []
    async with pool.write_transaction() as tx:
        for i in range(n):
            await tx.execute(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', ?, 't', 1, ?)",
                (f"{prefix}{i}", status),
            )
    for i in range(n):
        row = await pool.read_one(
            "SELECT id FROM games WHERE platform='steam' AND app_id=?", (f"{prefix}{i}",)
        )
        ids.append(int(row["id"]))
    return ids


async def _transitions(pool, game_id: int) -> list[dict]:
    return await pool.read_all(
        "SELECT prior, new_status, downward, commanded FROM measurement_transitions "
        "WHERE game_id=? ORDER BY id",
        (game_id,),
    )


async def test_a_commanded_measurement_writes_a_transition_row(pool):
    """The log is the audit trail for cache truth. A purge is cache truth."""
    (game_id,) = await _seed_games(pool, 1)

    await record_measurement(pool, game_id, "missing", commanded=True)

    rows = await _transitions(pool, game_id)
    assert len(rows) == 1, (
        "a commanded change wrote no transition row, so the largest deliberate cache "
        "change the system can make leaves no trace in the log that exists to record "
        "exactly that"
    )
    assert rows[0]["prior"] == "up_to_date"
    assert rows[0]["new_status"] == "not_downloaded"
    assert rows[0]["downward"] == 1, "losing a cached copy is a downward move however it happened"


async def test_a_commanded_transition_row_is_marked_as_commanded(pool):
    (game_id,) = await _seed_games(pool, 1)

    await record_measurement(pool, game_id, "missing", commanded=True)

    rows = await _transitions(pool, game_id)
    assert rows[0]["commanded"] == 1, (
        "unmarked, this row is indistinguishable from unexplained mass loss and the "
        "breaker will count it against the next sweep"
    )


async def test_an_observed_transition_row_is_not_marked(pool):
    (game_id,) = await _seed_games(pool, 1)

    await record_measurement(pool, game_id, "missing")

    rows = await _transitions(pool, game_id)
    assert rows[0]["commanded"] == 0


async def test_commanded_rows_do_not_arm_the_breaker_against_the_next_measurement(pool):
    """A library-sized purge must not refuse the next sweep's first honest answer.

    This is the false positive that justified writing no row at all. Marking the
    row keeps the history and still avoids it.
    """
    threshold = get_settings().measurement_breaker_threshold
    purged = await _seed_games(pool, threshold + 5, prefix="purged")
    for gid in purged:
        await record_measurement(pool, gid, "missing", commanded=True)

    (observed,) = await _seed_games(pool, 1, prefix="observed")
    await record_measurement(pool, observed, "missing")

    row = await pool.read_one("SELECT status FROM games WHERE id=?", (observed,))
    assert row["status"] == "not_downloaded", (
        f"{threshold + 5} deliberate purges armed the breaker and refused an unrelated "
        "game's honest measurement — an alarm the operator caused themselves"
    )


async def test_a_commanded_measurement_is_still_exempt_from_the_veto(pool):
    """The original SEV-2: the files are already gone, so refusing the write loses truth."""
    threshold = get_settings().measurement_breaker_threshold
    lost = await _seed_games(pool, threshold + 1, prefix="lost")
    with pytest.raises(CircuitBreakerTripped):
        for gid in lost:
            await record_measurement(pool, gid, "missing")

    (purged,) = await _seed_games(pool, 1, prefix="afterbreak")
    await record_measurement(pool, purged, "missing", commanded=True)

    row = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (purged,))
    assert row["status"] == "not_downloaded", (
        "a tripped breaker vetoed a purge's own record of files it had already deleted, "
        "leaving a green badge over an empty cache that the halted sweep cannot correct"
    )
    assert row["status_measured_at"] is not None


async def test_a_commanded_error_still_records_only_the_attempt(pool):
    """An infrastructure failure is not a measurement, whatever commanded it."""
    (game_id,) = await _seed_games(pool, 1)

    await record_measurement(pool, game_id, "error", commanded=True)

    row = await pool.read_one(
        "SELECT status, status_measured_at, last_measure_attempt_at FROM games WHERE id=?",
        (game_id,),
    )
    assert row["status"] == "up_to_date", "an error must never write cache truth"
    assert row["status_measured_at"] is None
    assert row["last_measure_attempt_at"] is not None
    assert await _transitions(pool, game_id) == [], "nothing transitioned, so nothing is logged"
