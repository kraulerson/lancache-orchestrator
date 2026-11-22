"""Migration 0019 — games.first_seen_at, the 'new_games_check' job kind, the
'trigger' job source, and the new_games_check in-flight dedup index.

Same shape as test_migration_0014_purge_kind.py: aiosqlite over an in-memory DB
(ADR-0001 DQ3 forbids synchronous sqlite3), prior migrations applied from the
packaged files, then the migration under test. aiosqlite.IntegrityError is
sqlite3.IntegrityError.
"""

from __future__ import annotations

import importlib.resources

import aiosqlite
import pytest

pytestmark = pytest.mark.asyncio

_MIGRATION = "0019_new_games_check.sql"

# Every jobs index that existed after 0014 (0015-0018 do not touch jobs).
_PRIOR_JOBS_INDEXES = {
    "idx_jobs_state_kind",
    "idx_jobs_started",
    "idx_jobs_finished",
    "idx_jobs_dedupe",
    "idx_jobs_library_sync_inflight",
    "idx_jobs_sweep_inflight",
    "idx_jobs_prefill_inflight",
    "idx_jobs_validate_inflight",
    "idx_jobs_manifest_fetch_inflight",
    "idx_jobs_fetch_manifests_inflight",
    "idx_jobs_purge_inflight",
}


def _migration_sql_through(stop_id: str) -> list[str]:
    root = importlib.resources.files("orchestrator.db.migrations")
    names = sorted(p.name for p in root.iterdir() if p.name.endswith(".sql"))
    return [root.joinpath(n).read_text(encoding="utf-8") for n in names if n[:4] <= stop_id]


def _sql(name: str) -> str:
    return (
        importlib.resources.files("orchestrator.db.migrations")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


async def _db_through_0018() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    for sql in _migration_sql_through("0018"):
        await conn.executescript(sql)
    return conn


async def _index_names(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("PRAGMA index_list(jobs)")
    rows = await cur.fetchall()
    await cur.close()
    return {r[1] for r in rows}


async def test_first_seen_at_column_exists_and_existing_rows_are_null() -> None:
    conn = await _db_through_0018()
    try:
        await conn.execute(
            "INSERT INTO games (platform, app_id, title, owned, status) "
            "VALUES ('steam', '440', 't', 1, 'up_to_date')"
        )
        await conn.commit()
        await conn.executescript(_sql(_MIGRATION))
        cur = await conn.execute("SELECT first_seen_at FROM games WHERE app_id = '440'")
        row = await cur.fetchone()
        await cur.close()
        assert row == (None,)
    finally:
        await conn.close()


async def test_jobs_accepts_new_games_check_kind_and_trigger_source() -> None:
    conn = await _db_through_0018()
    try:
        await conn.executescript(_sql(_MIGRATION))
        await conn.execute(
            "INSERT INTO jobs (kind, state, source) VALUES ('new_games_check', 'queued', 'trigger')"
        )
        await conn.commit()
        cur = await conn.execute("SELECT kind, source FROM jobs")
        assert await cur.fetchall() == [("new_games_check", "trigger")]
        await cur.close()
    finally:
        await conn.close()


async def test_jobs_rejects_unknown_kind_still() -> None:
    conn = await _db_through_0018()
    try:
        await conn.executescript(_sql(_MIGRATION))
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO jobs (kind, state, source) VALUES ('bogus', 'queued', 'api')"
            )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO jobs (kind, state, source) VALUES ('sweep', 'queued', 'bogus')"
            )
    finally:
        await conn.close()


async def test_new_games_check_inflight_dedup() -> None:
    conn = await _db_through_0018()
    try:
        await conn.executescript(_sql(_MIGRATION))
        sql = (
            "INSERT INTO jobs (kind, state, source) "
            "VALUES ('new_games_check', 'queued', 'trigger') ON CONFLICT DO NOTHING"
        )
        cur = await conn.execute(sql)
        assert cur.rowcount == 1
        await cur.close()
        cur = await conn.execute(sql)
        assert cur.rowcount == 0
        await cur.close()
        # Once the first finishes, a new one may queue.
        await conn.execute("UPDATE jobs SET state = 'succeeded'")
        cur = await conn.execute(sql)
        assert cur.rowcount == 1
        await cur.close()
    finally:
        await conn.close()


async def test_existing_jobs_rows_and_all_prior_indexes_survive() -> None:
    conn = await _db_through_0018()
    try:
        await conn.execute(
            "INSERT INTO jobs (id, kind, platform, state, source) "
            "VALUES (1, 'validate', 'steam', 'succeeded', 'scheduler')"
        )
        await conn.execute(
            "INSERT INTO jobs (id, kind, platform, state, source) "
            "VALUES (2, 'purge', 'steam', 'queued', 'gameshelf')"
        )
        await conn.commit()
        before_indexes = await _index_names(conn)
        assert before_indexes >= _PRIOR_JOBS_INDEXES

        await conn.executescript(_sql(_MIGRATION))

        cur = await conn.execute("SELECT id, kind, platform, state, source FROM jobs ORDER BY id")
        rows = await cur.fetchall()
        await cur.close()
        assert rows == [
            (1, "validate", "steam", "succeeded", "scheduler"),
            (2, "purge", "steam", "queued", "gameshelf"),
        ]
        after_indexes = await _index_names(conn)
        assert before_indexes <= after_indexes
        assert after_indexes - before_indexes == {"idx_jobs_new_games_check_inflight"}
    finally:
        await conn.close()
