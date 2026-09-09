"""Tests for migration 0015 — games measurement / job-outcome split.

Design 2026-09-04. Covers three behaviours:
  1. the new measurement and job-outcome columns land,
  2. the 2026-09-01 03:00-03:47 corruption repair touches ONLY the damaged rows —
     legitimately-cached rows inside the same window must survive,
  3. last_measure_attempt_at is backfilled from last_validated_at, so the
     measurement queue is ordered by real age instead of tying on NULL.

Follows the tests/db/test_migration_0014_purge_kind.py pattern: aiosqlite over an
in-memory DB, prior migrations applied from the packaged files, then the migration
under test. ADR-0001 DQ3 forbids synchronous sqlite3, so nothing here imports it.
"""

from __future__ import annotations

import importlib.resources

import aiosqlite
import pytest

pytestmark = pytest.mark.asyncio

_MIGRATION = "0015_games_measurement_split.sql"


def _migration_sql_through(stop_id: str) -> list[str]:
    """Return the SQL of every migration file whose 4-digit id is <= stop_id, in order."""
    root = importlib.resources.files("orchestrator.db.migrations")
    names = sorted(p.name for p in root.iterdir() if p.name.endswith(".sql"))
    return [root.joinpath(n).read_text(encoding="utf-8") for n in names if n[:4] <= stop_id]


def _sql(name: str) -> str:
    return (
        importlib.resources.files("orchestrator.db.migrations")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


async def _through_0014(conn: aiosqlite.Connection) -> None:
    for sql in _migration_sql_through("0014"):
        await conn.executescript(sql)


async def _games_cols(conn: aiosqlite.Connection) -> set[str]:
    # PRAGMA cannot take a `?` placeholder; the table name is a literal.
    cur = await conn.execute("PRAGMA table_info(games)")
    rows = await cur.fetchall()
    await cur.close()
    return {r[1] for r in rows}


async def test_0015_adds_measurement_columns() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0014(conn)
        await conn.executescript(_sql(_MIGRATION))

        cols = await _games_cols(conn)
        assert "status_measured_at" in cols
        assert "last_measure_attempt_at" in cols
        assert "last_job_outcome" in cols
        assert "last_job_outcome_at" in cols


async def test_0015_repairs_only_corrupted_rows() -> None:
    """The repair must not reset good measurements that fall inside the window.

    Epic up_to_date rows run from 03:30:09 — inside 03:00-03:47 — so a naive
    time-window reset would discard them for no gain.

    Rows 6 and 7 pin the window's edges: the predicate is
    ``>= '2026-09-01 03:00:00' AND <= '2026-09-01 03:47:59'``, so 03:00:00
    exactly is inside it and 03:48:00 is outside. Widening or narrowing either
    bound by a second now fails visibly instead of silently re-scoping a repair
    that runs once, against production, and cannot be undone.

    ``status_measured_at`` is asserted alongside ``status`` because the two must
    agree: a repaired row has no trustworthy measurement time (NULL), and a row
    the repair spared keeps the timestamp it was measured at.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0014(conn)
        # migration 0001 already seeds the platforms table; reference it, don't insert.
        await conn.executescript(
            """
            INSERT INTO games (id, platform, app_id, title, status, last_validated_at) VALUES
              (1, 'epic', 'a', 'Damaged partial', 'validation_failed', '2026-09-01 03:10:00'),
              (2, 'epic', 'b', 'Damaged failed',  'failed',            '2026-09-01 03:44:50'),
              (3, 'epic', 'c', 'Good in window',  'up_to_date',        '2026-09-01 03:30:09'),
              (4, 'steam', 'd', 'Old not_dl',     'not_downloaded',    '2026-06-18 23:46:33'),
              (5, 'epic', 'e', 'Outside window',  'validation_failed', '2026-08-30 12:00:00'),
              (6, 'epic', 'f', 'Window opens',    'validation_failed', '2026-09-01 03:00:00'),
              (7, 'epic', 'g', 'Window closed',   'validation_failed', '2026-09-01 03:48:00');
            """
        )
        await conn.commit()

        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute(
            "SELECT id, status, status_measured_at, last_validated_at FROM games ORDER BY id"
        )
        rows = {r[0]: r for r in await cur.fetchall()}
        await cur.close()

        status = {i: r[1] for i, r in rows.items()}
        assert status[1] == "unknown", "damaged validation_failed in window must reset"
        assert status[2] == "unknown", "damaged failed in window must reset"
        assert status[3] == "up_to_date", "a good measurement in the window must survive"
        assert status[4] == "not_downloaded", "a genuine stale measurement must survive"
        assert status[5] == "validation_failed", "outside the window must be untouched"
        assert status[6] == "unknown", "03:00:00 exactly is inside the window (>=)"
        assert status[7] == "validation_failed", "03:48:00 is past the window's 03:47:59 close"

        # A repaired row's measurement time is cleared: there is no trustworthy
        # moment at which its status was established.
        for gid in (1, 2, 6):
            assert rows[gid][2] is None, f"repaired row {gid} must have status_measured_at NULL"

        # A spared truth row keeps its measurement time, back-seeded from
        # last_validated_at so the split starts with the history it already had.
        for gid in (3, 4, 5, 7):
            assert rows[gid][2] == rows[gid][3], (
                f"untouched truth row {gid} must keep status_measured_at == last_validated_at"
            )


async def test_0015_backfills_attempt_from_last_validated() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0014(conn)
        await conn.executescript(
            """
            INSERT INTO games (id, platform, app_id, title, status, last_validated_at)
            VALUES (1, 'steam', '1', 'Old', 'not_downloaded', '2026-06-18 23:46:33');
            """
        )
        await conn.commit()

        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute("SELECT last_measure_attempt_at FROM games WHERE id = 1")
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == "2026-06-18 23:46:33"
