"""Migration 0012 — widen prefill_exclusions.source to allow 'gameshelf' (Piece 3, #446).

Two angles:
- The data-preservation rebuild (hand-applied 0011 then 0012 SQL to an in-memory
  connection) must keep every existing row and its values intact while widening
  the source CHECK.
- A fully-migrated DB (run through the real migrate framework, so CHECKSUMS/gap/
  post-apply-sanity all pass) must accept source='gameshelf' and still reject an
  unknown source and a duplicate (platform, app_id).

Uses aiosqlite (not synchronous sqlite3) per ADR-0001/DQ3 — the in-memory copy
below applies the real migration SQL and asserts on the resulting DB state.
"""

from __future__ import annotations

import importlib.resources

import aiosqlite
import pytest

from orchestrator.db.pool import PoolError


def _migration_sql(name: str) -> str:
    return (
        importlib.resources.files("orchestrator.db.migrations")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


async def test_0012_rebuild_preserves_existing_rows() -> None:
    """Applying 0011 then 0012 to a connection with live rows must preserve them
    (id, mode, reason, source) and widen the source constraint."""
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript(_migration_sql("0011_prefill_exclusions.sql"))
        await conn.execute(
            "INSERT INTO prefill_exclusions (platform, app_id, mode, reason, source) "
            "VALUES ('steam', '440', 'exclude', 'auto-classify: soundtrack', 'classifier')"
        )
        await conn.execute(
            "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
            "VALUES ('epic', 'keepme', 'allow', 'operator')"
        )
        await conn.commit()

        await conn.executescript(_migration_sql("0012_prefill_exclusions_gameshelf_source.sql"))

        cur = await conn.execute(
            "SELECT id, app_id, platform, mode, reason, source FROM prefill_exclusions"
        )
        rows = {r[1]: r for r in await cur.fetchall()}
        await cur.close()
        assert set(rows) == {"440", "keepme"}
        assert rows["440"][2:6] == ("steam", "exclude", "auto-classify: soundtrack", "classifier")
        assert rows["keepme"][2:6] == ("epic", "allow", None, "operator")

        # Constraint widened: 'gameshelf' now accepted.
        await conn.execute(
            "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
            "VALUES ('epic', 'gs1', 'exclude', 'gameshelf')"
        )
        # Unknown source still rejected.
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
                "VALUES ('epic', 'bad', 'exclude', 'nope')"
            )
        # UNIQUE(platform, app_id) still enforced.
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
                "VALUES ('steam', '440', 'exclude', 'gameshelf')"
            )


async def test_gameshelf_source_accepted_on_migrated_db(pool) -> None:
    """A DB run through the real migrate framework accepts source='gameshelf'."""
    await pool.execute_write(
        "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
        "VALUES ('epic', 'covered-by-steam', 'exclude', 'gameshelf')"
    )
    rows = await pool.read_all(
        "SELECT source FROM prefill_exclusions WHERE app_id = 'covered-by-steam'"
    )
    assert [r["source"] for r in rows] == ["gameshelf"]


async def test_unknown_source_still_rejected_on_migrated_db(pool) -> None:
    with pytest.raises(PoolError):
        await pool.execute_write(
            "INSERT INTO prefill_exclusions (platform, app_id, mode, source) "
            "VALUES ('epic', 'x', 'exclude', 'not-a-source')"
        )
