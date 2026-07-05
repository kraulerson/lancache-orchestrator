"""Migration 0014 — add the 'purge' job kind (F18 operator-driven cache purge).

Mirrors the 0009 STRICT-table rebuild (SQLite cannot ALTER a CHECK). Two angles:
- Row-preservation: apply every migration through 0013, seed jobs rows spanning
  existing kinds, apply 0014, and assert all rows survive intact while the widened
  CHECK accepts 'purge' and still rejects an unknown kind.
- Fully-migrated DB (real migrate framework via the `pool` fixture, so CHECKSUMS/
  gap/post-apply-sanity all pass): 'purge' accepted, unknown kind rejected.

Uses aiosqlite (not synchronous sqlite3) per ADR-0001/DQ3.
"""

from __future__ import annotations

import importlib.resources

import aiosqlite
import pytest

from orchestrator.db.pool import PoolError

pytestmark = pytest.mark.asyncio


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


async def test_0014_rebuild_preserves_jobs_and_adds_purge() -> None:
    """Applying every migration through 0013 then 0014 preserves existing jobs
    rows and widens the kind CHECK to include 'purge'."""
    async with aiosqlite.connect(":memory:") as conn:
        for sql in _migration_sql_through("0013"):
            await conn.executescript(sql)
        await conn.execute(
            "INSERT INTO jobs (id, kind, platform, state, source) "
            "VALUES (1, 'validate', 'steam', 'succeeded', 'scheduler')"
        )
        await conn.execute(
            "INSERT INTO jobs (id, kind, platform, state, source) "
            "VALUES (2, 'fetch_manifests', NULL, 'queued', 'api')"
        )
        await conn.commit()

        await conn.executescript(_sql("0014_jobs_kind_purge.sql"))

        cur = await conn.execute("SELECT id, kind, platform, state, source FROM jobs ORDER BY id")
        rows = await cur.fetchall()
        await cur.close()
        assert rows == [
            (1, "validate", "steam", "succeeded", "scheduler"),
            (2, "fetch_manifests", None, "queued", "api"),
        ]

        # Widened: 'purge' now accepted.
        await conn.execute(
            "INSERT INTO jobs (id, kind, game_id, platform, state, source) "
            "VALUES (3, 'purge', NULL, 'steam', 'queued', 'api')"
        )
        await conn.commit()
        # Unknown kind still rejected.
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO jobs (id, kind, state, source) VALUES (4, 'bogus', 'queued', 'api')"
            )


async def test_purge_kind_accepted_on_migrated_db(pool) -> None:
    """A DB run through the real migrate framework accepts kind='purge'."""
    await pool.execute_write(
        "INSERT INTO jobs (kind, platform, state, source) "
        "VALUES ('purge', 'steam', 'queued', 'api')"
    )
    rows = await pool.read_all("SELECT kind FROM jobs WHERE kind='purge'")
    assert [r["kind"] for r in rows] == ["purge"]


async def test_unknown_kind_still_rejected_on_migrated_db(pool) -> None:
    with pytest.raises(PoolError):
        await pool.execute_write(
            "INSERT INTO jobs (kind, state, source) VALUES ('nope', 'queued', 'api')"
        )
