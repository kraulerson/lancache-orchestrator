"""Tests for migration 0017 — the sweep pass marker.

#311. A full validation pass needs ~12.5 h against a 6 h job cap, so no single
sweep run can ever cover the library and `sweep.completed` can never fire. The
fix is a pass boundary that spans runs: one row recording which pass is in
flight and when it began, so candidates become "not yet attempted THIS pass"
rather than "not attempted recently".

The table is deliberately a single row (`CHECK (id = 1)`): there is exactly one
sweep pass in flight at a time, and a schema that cannot express a second one
cannot drift into expressing one by accident.

Follows the tests/db/test_migration_0016_commanded_transitions.py pattern:
aiosqlite over an in-memory DB, prior migrations applied from the packaged
files, then the migration under test. ADR-0001 DQ3 forbids synchronous sqlite3,
so nothing here imports it.
"""

from __future__ import annotations

import importlib.resources

import aiosqlite
import pytest

pytestmark = pytest.mark.asyncio

_MIGRATION = "0017_sweep_pass.sql"


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


async def _through_0016(conn: aiosqlite.Connection) -> None:
    for sql in _migration_sql_through("0016"):
        await conn.executescript(sql)


async def _tables(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    rows = await cur.fetchall()
    await cur.close()
    return {r[0] for r in rows}


async def test_0017_creates_the_sweep_pass_table() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0016(conn)
        assert "sweep_pass" not in await _tables(conn), (
            "precondition: 0016 must not already have this table, or the test proves nothing"
        )

        await conn.executescript(_sql(_MIGRATION))

        assert "sweep_pass" in await _tables(conn)


async def test_0017_seeds_pass_one_so_every_game_is_a_candidate() -> None:
    """The marker must arrive already populated.

    A handler that has to cope with an empty marker table would need an
    initialise-on-first-read path — a second place where a pass can begin, and
    one that runs under whatever concurrency the sweep happens to have. Seeding
    it here means there is exactly one such place, and it is this migration.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0016(conn)
        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute("SELECT id, pass_number, pass_started_at FROM sweep_pass")
        rows = await cur.fetchall()
        await cur.close()

        assert len(rows) == 1, "exactly one pass is ever in flight"
        assert rows[0][0] == 1
        assert rows[0][1] == 1, "the deployed system starts on pass 1"
        assert rows[0][2], "pass_started_at must be stamped, not NULL"


async def test_0017_refuses_a_second_pass_row() -> None:
    """Two rows would make "the current pass" ambiguous, and the candidate query
    would silently pick one of them."""
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0016(conn)
        await conn.executescript(_sql(_MIGRATION))

        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO sweep_pass (id, pass_number, pass_started_at) "
                "VALUES (2, 1, CURRENT_TIMESTAMP)"
            )


async def test_0017_seeded_stamp_precedes_every_existing_attempt_stamp() -> None:
    """Games measured BEFORE the migration must all be candidates for pass 1.

    The live DB has 3212 owned games, none with a NULL last_measure_attempt_at
    (the resumable ordering has kept them all stamped). If the seeded
    pass_started_at sorted before those stamps, pass 1 would begin believing
    most of the library was already covered and would "complete" within minutes
    without measuring anything — the exact false green this work exists to
    prevent.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0016(conn)
        # A game measured "before the migration", stamped the way measurement.py
        # stamps it.
        cur = await conn.execute(
            "INSERT INTO games (platform, app_id, title, owned, status, last_measure_attempt_at) "
            "VALUES ('steam', '440', 't', 1, 'up_to_date', CURRENT_TIMESTAMP)"
        )
        await cur.close()
        await conn.commit()

        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute(
            "SELECT COUNT(*) FROM games, sweep_pass "
            "WHERE games.last_measure_attempt_at IS NULL "
            "   OR games.last_measure_attempt_at <= sweep_pass.pass_started_at"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == 1, (
            "a game measured before the migration must be a candidate for pass 1"
        )
