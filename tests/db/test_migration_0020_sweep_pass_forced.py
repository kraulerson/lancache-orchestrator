"""Tests for migration 0020 — sweep_pass gains `forced` and `last_eviction_ts`.

Feature B (eviction re-check). Follows the 0017 test pattern: aiosqlite over an
in-memory DB, prior migrations applied from the packaged files, then 0020.
"""

from __future__ import annotations

import importlib.resources

import aiosqlite
import pytest

pytestmark = pytest.mark.asyncio


def _migration_sql_through(stop_id: str) -> list[str]:
    root = importlib.resources.files("orchestrator.db.migrations")
    names = sorted(p.name for p in root.iterdir() if p.name.endswith(".sql"))
    return [root.joinpath(n).read_text(encoding="utf-8") for n in names if n[:4] <= stop_id]


async def _apply_through(conn: aiosqlite.Connection, stop_id: str) -> None:
    for sql in _migration_sql_through(stop_id):
        await conn.executescript(sql)


async def test_0020_adds_forced_and_last_eviction_ts_with_safe_defaults() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _apply_through(conn, "0019")
        cur = await conn.execute("SELECT pass_number, pass_started_at FROM sweep_pass WHERE id=1")
        before = await cur.fetchone()
        await _apply_through_only(conn, "0020")
        cur = await conn.execute(
            "SELECT pass_number, pass_started_at, forced, last_eviction_ts "
            "FROM sweep_pass WHERE id=1"
        )
        row = await cur.fetchone()
        assert row[0] == before[0] and row[1] == before[1], "existing pass must be untouched"
        assert row[2] == 0
        assert row[3] is None


async def test_0020_forced_rejects_values_other_than_0_and_1() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _apply_through(conn, "0020")
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute("UPDATE sweep_pass SET forced = 2 WHERE id = 1")


async def test_0020_checksum_is_recorded() -> None:
    import hashlib

    root = importlib.resources.files("orchestrator.db.migrations")
    body = root.joinpath("0020_sweep_pass_forced.sql").read_bytes()
    checksums = root.joinpath("CHECKSUMS").read_text(encoding="utf-8")
    assert f"0020  {hashlib.sha256(body).hexdigest()}  0020_sweep_pass_forced.sql" in checksums


async def _apply_through_only(conn: aiosqlite.Connection, migration_id: str) -> None:
    root = importlib.resources.files("orchestrator.db.migrations")
    names = sorted(p.name for p in root.iterdir() if p.name.endswith(".sql"))
    for n in names:
        if n[:4] == migration_id:
            await conn.executescript(root.joinpath(n).read_text(encoding="utf-8"))
