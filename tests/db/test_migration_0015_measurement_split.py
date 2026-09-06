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
              (5, 'epic', 'e', 'Outside window',  'validation_failed', '2026-08-30 12:00:00');
            """
        )
        await conn.commit()

        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute("SELECT id, status FROM games ORDER BY id")
        rows = dict(await cur.fetchall())
        await cur.close()

        assert rows[1] == "unknown", "damaged validation_failed in window must reset"
        assert rows[2] == "unknown", "damaged failed in window must reset"
        assert rows[3] == "up_to_date", "a good measurement in the window must survive"
        assert rows[4] == "not_downloaded", "a genuine stale measurement must survive"
        assert rows[5] == "validation_failed", "outside the window must be untouched"


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
