"""Tests for migration 0018 — retire the unreachable 'failed' cache status (#316).

19 owned games carry ``status='failed'`` with ``status_measured_at IS NULL`` and
no code path can ever clear them. No module writes ``'failed'`` any more —
``grep`` finds it only in comments — and 0015's repair predicate required a
``last_validated_at`` inside its window, which these rows do not have. They ARE
re-attempted daily, but every attempt returns an error, which correctly takes the
attempt-only path and writes no cache truth. So the stale label survives forever.

Verified live 2026-09-20 before writing this, because the operator's instruction
was conditional on the games genuinely being undownloadable. They are, by two
different mechanisms:

  - 15 Epic rows fail with ``EpicManifestError: epic manifest API failed: HTTP 4xx``
    and include titles Epic cannot serve at all (EA App titles, a mobile SKU, a
    delisted game).
  - 4 Steam rows fail with ``no_manifest_in_cache`` and are absent from
    SteamPrefill's 1192-app selection, so nothing ever downloads them and no
    manifest can ever land. Three of the four are tools rather than games.

``'blocked'`` is therefore the honest terminal state: already legal in the 0001
CHECK constraint, already excluded from prefill, and — unlike ``'unknown'`` — it
does not imply the system merely has not looked yet.

Follows the tests/db/test_migration_0016_commanded_transitions.py pattern:
aiosqlite over an in-memory DB, prior migrations applied from the packaged files,
then the migration under test. ADR-0001 DQ3 forbids synchronous sqlite3.
"""

from __future__ import annotations

import importlib.resources

import aiosqlite
import pytest

pytestmark = pytest.mark.asyncio

_MIGRATION = "0018_retire_failed_status.sql"


def _migration_sql_through(stop_id: str) -> list[str]:
    """Return the SQL of every migration whose 4-digit id is <= stop_id, in order."""
    root = importlib.resources.files("orchestrator.db.migrations")
    names = sorted(p.name for p in root.iterdir() if p.name.endswith(".sql"))
    return [root.joinpath(n).read_text(encoding="utf-8") for n in names if n[:4] <= stop_id]


def _sql(name: str) -> str:
    return (
        importlib.resources.files("orchestrator.db.migrations")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


async def _through_0017(conn: aiosqlite.Connection) -> None:
    for sql in _migration_sql_through("0017"):
        await conn.executescript(sql)


async def _seed_game(
    conn: aiosqlite.Connection,
    app_id: str,
    status: str,
    *,
    platform: str = "steam",
    owned: int = 1,
    measured_at: str | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO games (platform, app_id, title, owned, status, status_measured_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (platform, app_id, f"game-{app_id}", owned, status, measured_at),
    )
    game_id = cur.lastrowid
    await cur.close()
    assert game_id is not None
    return game_id


async def _status_of(conn: aiosqlite.Connection, game_id: int) -> str:
    cur = await conn.execute("SELECT status FROM games WHERE id = ?", (game_id,))
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    return str(row[0])


async def test_a_failed_row_becomes_blocked() -> None:
    """The whole point: a status no code can produce and no code can clear becomes
    one that says what is actually true — we will not be downloading this."""
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0017(conn)
        stuck = await _seed_game(conn, "340", "failed")

        await conn.executescript(_sql(_MIGRATION))

        assert await _status_of(conn, stuck) == "blocked"


async def test_every_other_status_is_left_alone() -> None:
    """A migration that rewrites cache truth must touch only the rows it claims to.
    0015's reset moved 1753 rows; the blast radius of a mistake here is the whole
    library's measured state."""
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0017(conn)
        untouched = {}
        for i, status in enumerate(
            ("unknown", "not_downloaded", "up_to_date", "pending_update", "validation_failed")
        ):
            untouched[status] = await _seed_game(conn, f"90{i}", status)

        await conn.executescript(_sql(_MIGRATION))

        for status, game_id in untouched.items():
            assert await _status_of(conn, game_id) == status


async def test_an_already_blocked_row_is_not_disturbed() -> None:
    """Re-running must be a no-op, and a row blocked for an unrelated reason must
    not acquire this migration's transition row."""
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0017(conn)
        already = await _seed_game(conn, "999", "blocked")

        await conn.executescript(_sql(_MIGRATION))

        assert await _status_of(conn, already) == "blocked"
        cur = await conn.execute(
            "SELECT count(*) FROM measurement_transitions WHERE game_id = ?", (already,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == 0


async def test_the_change_is_recorded_as_a_commanded_transition() -> None:
    """Every status change owes an audit row, and this one must carry commanded=1.

    0016 exists precisely so a deliberate operator action does not sit in the
    breaker's rolling window as if it were cache loss. A migration retiring 19
    rows is the most deliberate action there is; left uncommanded it would be 19
    downward transitions arriving at once and could veto the next honest sweep.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0017(conn)
        stuck = await _seed_game(conn, "340", "failed")

        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute(
            "SELECT prior, new_status, downward, commanded "
            "FROM measurement_transitions WHERE game_id = ?",
            (stuck,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None, "the migration must leave an audit trail"
        prior, new_status, _downward, commanded = row
        assert prior == "failed"
        assert new_status == "blocked"
        assert commanded == 1, "a migration is commanded by definition; see #310/0016"


async def test_status_measured_at_stays_null() -> None:
    """'blocked' is a policy decision, not a measurement.

    Stamping status_measured_at would assert the cache was inspected and found in
    this state, which is exactly the truth/outcome conflation migration 0015 was
    written to end. Epic's manifest API refusing us is not a cache observation.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0017(conn)
        stuck = await _seed_game(conn, "340", "failed")

        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute("SELECT status_measured_at FROM games WHERE id = ?", (stuck,))
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] is None


async def test_failed_rows_that_were_measured_are_still_migrated() -> None:
    """Do not predicate on status_measured_at IS NULL.

    That is the mistake 0015's repair made: it filtered on a column these rows did
    not have, so it silently skipped them and left the defect to be found in UAT 15
    weeks later. 'failed' is unreachable for every row regardless of how it got
    there, so the predicate is the status alone.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0017(conn)
        measured = await _seed_game(conn, "341", "failed", measured_at="2026-09-01 12:00:00")

        await conn.executescript(_sql(_MIGRATION))

        assert await _status_of(conn, measured) == "blocked"


async def test_unowned_rows_are_migrated_too() -> None:
    """Ownership can flip back. A row left at 'failed' because it was unowned on
    migration day would become a fresh instance of this same bug the moment the
    library sync re-owned it."""
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0017(conn)
        unowned = await _seed_game(conn, "342", "failed", owned=0)

        await conn.executescript(_sql(_MIGRATION))

        assert await _status_of(conn, unowned) == "blocked"


async def test_the_migration_is_idempotent() -> None:
    """Applying twice must not double the audit rows.

    The runner will not re-run a recorded migration, but a restore-and-replay is a
    real operational path, and a second run must not fabricate a 'blocked ->
    blocked' transition.
    """
    async with aiosqlite.connect(":memory:") as conn:
        await _through_0017(conn)
        stuck = await _seed_game(conn, "340", "failed")

        await conn.executescript(_sql(_MIGRATION))
        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute(
            "SELECT count(*) FROM measurement_transitions WHERE game_id = ?", (stuck,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == 1
