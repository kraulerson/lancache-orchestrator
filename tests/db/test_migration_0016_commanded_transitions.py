"""Tests for migration 0016 — mark a transition row as commanded.

#310. The purge path is moving from "write no transition row at all" to "write a
real one, but stay exempt from the breaker's veto". Without a way to tell the two
apart, a bulk purge would leave its own downward rows inside the breaker's rolling
window and refuse the next sweep's first honest measurement — a false alarm caused
by the operator's own deliberate action, which is how a breaker stops being
trusted.

So the row carries a flag, and the breaker's count and its partial index both
exclude it.

Follows the tests/db/test_migration_0015_measurement_split.py pattern: aiosqlite
over an in-memory DB, prior migrations applied from the packaged files, then the
migration under test. ADR-0001 DQ3 forbids synchronous sqlite3, so nothing here
imports it.
"""

from __future__ import annotations

import importlib.resources

import aiosqlite
import pytest

pytestmark = pytest.mark.asyncio

_MIGRATION = "0016_commanded_transitions.sql"


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


async def _through_0015(conn: aiosqlite.Connection) -> None:
    for sql in _migration_sql_through("0015"):
        await conn.executescript(sql)


async def _transition_cols(conn: aiosqlite.Connection) -> set[str]:
    # PRAGMA cannot take a `?` placeholder; the table name is a literal.
    cur = await conn.execute("PRAGMA table_info(measurement_transitions)")
    rows = await cur.fetchall()
    await cur.close()
    return {r[1] for r in rows}


async def _seed_game(conn: aiosqlite.Connection, app_id: str = "440") -> int:
    cur = await conn.execute(
        "INSERT INTO games (platform, app_id, title, owned, status) "
        "VALUES ('steam', ?, 't', 1, 'up_to_date')",
        (app_id,),
    )
    await cur.close()
    cur = await conn.execute("SELECT id FROM games WHERE app_id=?", (app_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    return int(row[0])


async def test_0016_adds_the_commanded_column() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0015(conn)
        assert "commanded" not in await _transition_cols(conn), (
            "precondition: 0015 must not already have this column, or the test proves nothing"
        )

        await conn.executescript(_sql(_MIGRATION))

        assert "commanded" in await _transition_cols(conn)


async def test_0016_defaults_existing_and_new_rows_to_observed() -> None:
    """Every row written before this migration was an observation, never a command."""
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0015(conn)
        game_id = await _seed_game(conn)
        await conn.execute(
            "INSERT INTO measurement_transitions (game_id, prior, new_status, downward) "
            "VALUES (?, 'up_to_date', 'not_downloaded', 1)",
            (game_id,),
        )
        await conn.commit()

        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute("SELECT commanded FROM measurement_transitions")
        rows = await cur.fetchall()
        await cur.close()
        assert [r[0] for r in rows] == [0], "a pre-existing row is an observation"

        # And a row inserted without naming the column is one too.
        await conn.execute(
            "INSERT INTO measurement_transitions (game_id, prior, new_status, downward) "
            "VALUES (?, 'up_to_date', 'validation_failed', 1)",
            (game_id,),
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT commanded FROM measurement_transitions ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 0


async def test_0016_rejects_a_commanded_value_that_is_not_a_flag() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0015(conn)
        game_id = await _seed_game(conn)
        await conn.executescript(_sql(_MIGRATION))

        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO measurement_transitions "
                "(game_id, prior, new_status, downward, commanded) "
                "VALUES (?, 'up_to_date', 'not_downloaded', 1, 2)",
                (game_id,),
            )


async def test_0016_rebuilds_the_breaker_index_to_exclude_commanded_rows() -> None:
    """The breaker's window query is index-backed; the index must match its predicate.

    If the partial index still reads `WHERE downward = 1` alone, a library-sized
    purge fills it with rows the breaker is then required to filter out by hand on
    every measurement.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0015(conn)
        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_measurement_transitions_window'"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None, "the breaker's window index must still exist"
        sql = " ".join(str(row[0]).split()).lower()
        assert "commanded = 0" in sql, f"index still counts commanded rows: {sql}"
        assert "downward = 1" in sql, f"index no longer restricted to losses: {sql}"
