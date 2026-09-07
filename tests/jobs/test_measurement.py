"""The single truth writer: orchestrator.jobs.measurement.

Cache truth (``games.status`` + ``games.status_measured_at``) may only be
written here. These tests pin the split the 2026-09-04 design requires: a real
measurement writes truth, an infrastructure error records only the attempt, and
a job outcome never touches truth at all.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from orchestrator.jobs.measurement import record_job_outcome, record_measurement

pytestmark = pytest.mark.asyncio

# A measurement timestamp already on the row. Asserting the error path leaves
# THIS value in place is a real assertion; asserting a column that was NULL from
# birth is still NULL proves nothing.
SEEDED_MEASURED_AT = "2026-09-01 00:00:00"


async def _seed(
    pool, *, status="up_to_date", platform="steam", app_id="730", measured_at=None
) -> int:
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, status_measured_at) "
        "VALUES (?, ?, 't', 1, ?, ?)",
        (platform, app_id, status, measured_at),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return int(row["id"])


async def test_error_outcome_never_writes_truth(pool):
    game_id = await _seed(pool, status="up_to_date", measured_at=SEEDED_MEASURED_AT)

    await record_measurement(pool, game_id, "error")

    row = await pool.read_one(
        "SELECT status, status_measured_at, last_measure_attempt_at FROM games WHERE id=?",
        (game_id,),
    )
    assert row["status"] == "up_to_date"  # truth untouched
    assert row["status_measured_at"] == SEEDED_MEASURED_AT  # not re-stamped as measured
    assert row["last_measure_attempt_at"] is not None  # attempt WAS recorded


async def test_success_writes_truth_and_attempt(pool):
    game_id = await _seed(pool, status="unknown")

    await record_measurement(pool, game_id, "cached")

    row = await pool.read_one(
        "SELECT status, status_measured_at, last_measure_attempt_at, last_validated_at "
        "FROM games WHERE id=?",
        (game_id,),
    )
    assert row["status"] == "up_to_date"
    assert row["status_measured_at"] is not None
    assert row["last_measure_attempt_at"] is not None
    # Existing readers (API, CLI, sweep ordering) still key off last_validated_at.
    assert row["last_validated_at"] is not None


async def test_partial_maps_to_validation_failed(pool):
    game_id = await _seed(pool, status="unknown")

    await record_measurement(pool, game_id, "partial")

    row = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert row["status"] == "validation_failed"


async def test_missing_maps_to_not_downloaded(pool):
    """'missing' is distinct from 'partial': nothing on disk is not a failed
    validation, and the circuit breaker (Task 3) ranks the two differently."""
    game_id = await _seed(pool, status="unknown")

    await record_measurement(pool, game_id, "missing")

    row = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert row["status"] == "not_downloaded"


async def test_unknown_outcome_records_attempt_only(pool):
    """Anything not in the status map is treated like 'error' — never truth."""
    game_id = await _seed(pool, status="up_to_date", measured_at=SEEDED_MEASURED_AT)

    await record_measurement(pool, game_id, "banana")

    row = await pool.read_one(
        "SELECT status, status_measured_at, last_measure_attempt_at FROM games WHERE id=?",
        (game_id,),
    )
    assert row["status"] == "up_to_date"
    assert row["status_measured_at"] == SEEDED_MEASURED_AT
    assert row["last_measure_attempt_at"] is not None


async def test_unrecognised_outcome_logs_a_warning(pool):
    """'error' is expected and stays at info. Anything else is a caller bug: a
    typo like 'cahced' silently stops writing truth for every game it touches,
    so it must be loud enough to see."""
    game_id = await _seed(pool, status="up_to_date")

    with capture_logs() as logs:
        await record_measurement(pool, game_id, "cahced")
        await record_measurement(pool, game_id, "error")

    levels = {entry["outcome"]: entry["log_level"] for entry in logs}
    assert levels["cahced"] == "warning"
    assert levels["error"] == "info"


async def test_job_outcome_never_touches_status(pool):
    game_id = await _seed(pool, status="up_to_date")

    await record_job_outcome(pool, game_id, "prefill interrupted")

    row = await pool.read_one(
        "SELECT status, status_measured_at, last_job_outcome, last_job_outcome_at "
        "FROM games WHERE id=?",
        (game_id,),
    )
    assert row["status"] == "up_to_date"
    assert row["status_measured_at"] is None
    assert row["last_job_outcome"] == "prefill interrupted"
    assert row["last_job_outcome_at"] is not None


async def test_job_outcome_is_truncated(pool):
    """The column is free text from a subprocess; cap it like every other
    operator-facing error string in this codebase."""
    game_id = await _seed(pool)

    await record_job_outcome(pool, game_id, "x" * 500)

    row = await pool.read_one("SELECT last_job_outcome FROM games WHERE id=?", (game_id,))
    assert row["last_job_outcome"] == "x" * 200


async def test_writes_join_an_open_transaction(pool):
    """Both writers accept an already-open WriteTx so a caller can flip status
    and record a related row atomically (the #293 class of bug)."""
    game_id = await _seed(pool, status="unknown")

    async with pool.write_transaction() as tx:
        await record_measurement(pool, game_id, "partial", tx=tx)
        await record_job_outcome(pool, game_id, "sweep finished", tx=tx)
        # Still inside the transaction: an outside reader sees nothing yet.
        mid = await pool.read_one(
            "SELECT status, last_job_outcome FROM games WHERE id=?", (game_id,)
        )
        assert mid["status"] == "unknown"
        assert mid["last_job_outcome"] is None

    row = await pool.read_one(
        "SELECT status, status_measured_at, last_job_outcome FROM games WHERE id=?",
        (game_id,),
    )
    assert row["status"] == "validation_failed"
    assert row["status_measured_at"] is not None
    assert row["last_job_outcome"] == "sweep finished"


async def test_transaction_rollback_discards_the_measurement(pool):
    """The tx path must really be inside the caller's transaction — a rollback
    takes the status write with it."""
    game_id = await _seed(pool, status="unknown")

    with pytest.raises(RuntimeError, match="caller exploded"):
        async with pool.write_transaction() as tx:
            await record_measurement(pool, game_id, "cached", tx=tx)
            raise RuntimeError("caller exploded")

    row = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (game_id,))
    assert row["status"] == "unknown"
    assert row["status_measured_at"] is None
