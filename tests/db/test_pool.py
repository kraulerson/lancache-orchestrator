"""Tests for orchestrator.db.pool — async DB pool (BL4).

Mirrors the spec category breakdown:
  TestLifecycle      — Pool.create / close / context manager / health_check
  TestSchemaIntegration — verify_schema_current invocation, schema_status, escape hatch
  TestSingleStatementHelpers — read_one/all/stream, execute_write/many_write
  TestDataclassMapping — read_one_as / read_all_as
  TestReadTransaction — multi-statement read context
  TestWriteTransaction — multi-statement write context, commit/rollback
  TestRawAcquire — acquire_reader / acquire_writer escape hatches
  TestErrorWrapping — every exception type triggered + scrubbing
  TestModuleSingleton — init_pool / get_pool / reload_pool / close_pool
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from orchestrator.db.pool import (
    IntegrityViolationError,
    Pool,
    PoolClosedError,
    PoolInitError,
    PoolNotInitializedError,
    QuerySyntaxError,
    SchemaNotMigratedError,
    close_pool,
    get_pool,
    init_pool,
    reload_pool,
)

if TYPE_CHECKING:
    from pathlib import Path


VALID_TOKEN = "a" * 32


# ----------------------------------------------------------------------
# 1. Lifecycle
# ----------------------------------------------------------------------


class TestLifecycle:
    async def test_create_opens_writer_and_n_readers(self, db_path: Path):
        async with Pool.create(database_path=db_path, readers_count=4) as pool:
            health = await pool.health_check()
            assert health["writer"]["healthy"] is True
            assert health["readers"]["total"] == 4
            assert health["readers"]["healthy"] == 4

    async def test_create_with_skip_schema_verify_succeeds_on_empty_db(self, tmp_path: Path):
        empty_db = tmp_path / "empty.db"
        empty_db.touch()  # zero-byte SQLite file
        async with Pool.create(
            database_path=empty_db,
            readers_count=2,
            skip_schema_verify=True,
        ) as pool:
            assert pool is not None

    async def test_create_fails_on_unmigrated_db(self, tmp_path: Path):
        empty_db = tmp_path / "empty.db"
        empty_db.touch()
        with pytest.raises(SchemaNotMigratedError):
            async with Pool.create(database_path=empty_db, readers_count=2):
                pass

    async def test_close_idempotent(self, db_path: Path):
        pool = await Pool.create(database_path=db_path, readers_count=2)
        await pool.close()
        await pool.close()  # second close is a no-op, must not raise

    async def test_use_after_close_raises(self, db_path: Path):
        pool = await Pool.create(database_path=db_path, readers_count=2)
        await pool.close()
        with pytest.raises(PoolClosedError):
            await pool.read_one("SELECT 1")

    async def test_context_manager_closes_on_exit(self, db_path: Path):
        async with Pool.create(database_path=db_path, readers_count=2) as pool:
            await pool.read_one("SELECT 1")
        # After exit, pool is closed
        with pytest.raises(PoolClosedError):
            await pool.read_one("SELECT 1")

    async def test_context_manager_closes_on_exception(self, db_path: Path):
        captured = None
        try:
            async with Pool.create(database_path=db_path, readers_count=2) as pool:
                captured = pool
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with pytest.raises(PoolClosedError):
            await captured.read_one("SELECT 1")

    async def test_pool_init_error_includes_role_on_pragma_fail(self, tmp_path: Path, monkeypatch):
        """If a PRAGMA verification fails, PoolInitError carries the role."""
        # Patch _pragma_value_matches to always return False
        from orchestrator.db import pool as pool_mod

        monkeypatch.setattr(pool_mod, "_pragma_value_matches", lambda *_args: False)

        empty_db = tmp_path / "empty.db"
        empty_db.touch()
        with pytest.raises(PoolInitError) as exc_info:
            async with Pool.create(
                database_path=empty_db,
                readers_count=2,
                skip_schema_verify=True,
            ):
                pass
        assert exc_info.value.role in ("writer", "reader")


# ----------------------------------------------------------------------
# 2. Schema integration
# ----------------------------------------------------------------------


class TestSchemaIntegration:
    async def test_schema_status_returns_current_true(self, pool: Pool):
        status = await pool.schema_status()
        assert status["current"] is True
        assert status["pending"] == []
        assert status["unknown"] == []
        assert 1 in status["applied"]

    async def test_schema_status_shape(self, pool: Pool):
        status = await pool.schema_status()
        assert set(status.keys()) == {"applied", "available", "pending", "unknown", "current"}
        assert isinstance(status["applied"], list)
        assert isinstance(status["available"], list)

    async def test_skip_schema_verify_emits_warning(self, db_path: Path, capsys):
        from orchestrator.core import logging as log_mod

        log_mod.configure_logging()
        async with Pool.create(
            database_path=db_path,
            readers_count=2,
            skip_schema_verify=True,
        ):
            pass
        out = capsys.readouterr().out
        events = [json.loads(line)["event"] for line in out.splitlines() if line.strip()]
        assert "pool.schema_verification_skipped" in events


# ----------------------------------------------------------------------
# 3. Single-statement helpers
# ----------------------------------------------------------------------


class TestSingleStatementHelpers:
    async def test_read_one_returns_dict(self, populated_pool: Pool):
        row = await populated_pool.read_one(
            "SELECT id, platform, app_id, title FROM games WHERE id = ?", (1,)
        )
        assert row is not None
        assert isinstance(row, dict)
        assert row["platform"] == "steam"
        assert row["app_id"] == "10"

    async def test_read_one_returns_none_on_no_match(self, populated_pool: Pool):
        row = await populated_pool.read_one("SELECT id FROM games WHERE id = ?", (9999,))
        assert row is None

    async def test_read_all_returns_list_of_dicts(self, populated_pool: Pool):
        rows = await populated_pool.read_all(
            "SELECT id, platform FROM games WHERE platform = ? ORDER BY id",
            ("steam",),
        )
        assert len(rows) == 3
        assert all(isinstance(r, dict) for r in rows)
        assert [r["platform"] for r in rows] == ["steam"] * 3

    async def test_read_all_empty(self, populated_pool: Pool):
        rows = await populated_pool.read_all("SELECT id FROM games WHERE platform = ?", ("nope",))
        assert rows == []

    async def test_execute_write_inserts(self, populated_pool: Pool):
        rowcount = await populated_pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned, status) "
            "VALUES (?, ?, ?, 1, 'never_prefilled')",
            ("steam", "999", "Test Game"),
        )
        assert rowcount == 1
        rows = await populated_pool.read_all("SELECT title FROM games WHERE app_id = ?", ("999",))
        assert rows[0]["title"] == "Test Game"

    async def test_execute_write_returns_zero_on_no_match(self, pool: Pool):
        rowcount = await pool.execute_write("UPDATE games SET title = 'x' WHERE id = ?", (9999,))
        assert rowcount == 0

    async def test_execute_many_write_bulk_insert(self, pool: Pool):
        rows_data = [
            ("steam", str(1000 + i), f"Bulk Game {i}", 1, "never_prefilled") for i in range(10)
        ]
        rowcount = await pool.execute_many_write(
            "INSERT INTO games (platform, app_id, title, owned, status) VALUES (?, ?, ?, ?, ?)",
            rows_data,
        )
        assert rowcount == 10
        result = await pool.read_one("SELECT COUNT(*) AS c FROM games WHERE platform = 'steam'")
        assert result["c"] >= 10

    async def test_execute_many_write_atomicity(self, pool: Pool):
        """If one row in the batch fails (e.g., constraint violation), entire
        batch must roll back."""
        # First insert succeeds; second has a constraint violation (duplicate
        # platform+app_id with the existing row 1)
        await pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned, status) "
            "VALUES ('steam', 'unique-1', 'First', 1, 'never_prefilled')"
        )
        rows_data = [
            ("steam", "unique-2", "OK", 1, "never_prefilled"),
            ("steam", "unique-1", "DUPE", 1, "never_prefilled"),  # will fail
        ]
        with pytest.raises(IntegrityViolationError):
            await pool.execute_many_write(
                "INSERT INTO games (platform, app_id, title, owned, status) VALUES (?, ?, ?, ?, ?)",
                rows_data,
            )
        # Verify neither was inserted
        result = await pool.read_one(
            "SELECT COUNT(*) AS c FROM games "
            "WHERE app_id IN ('unique-2', 'unique-1') AND title != 'First'"
        )
        assert result["c"] == 0


# ----------------------------------------------------------------------
# 4. Dataclass mapping helpers
# ----------------------------------------------------------------------


@dataclass
class GameRow:
    id: int
    platform: str
    app_id: str
    title: str


class TestDataclassMapping:
    async def test_read_one_as_returns_dataclass(self, populated_pool: Pool):
        game = await populated_pool.read_one_as(
            GameRow,
            "SELECT id, platform, app_id, title FROM games WHERE id = ?",
            (1,),
        )
        assert game is not None
        assert isinstance(game, GameRow)
        assert game.platform == "steam"
        assert game.app_id == "10"

    async def test_read_one_as_returns_none_on_no_match(self, populated_pool: Pool):
        game = await populated_pool.read_one_as(
            GameRow,
            "SELECT id, platform, app_id, title FROM games WHERE id = ?",
            (9999,),
        )
        assert game is None

    async def test_read_all_as_returns_list_of_dataclasses(self, populated_pool: Pool):
        games = await populated_pool.read_all_as(
            GameRow,
            "SELECT id, platform, app_id, title FROM games WHERE platform = ? ORDER BY id",
            ("epic",),
        )
        assert len(games) == 2
        assert all(isinstance(g, GameRow) for g in games)
        assert {g.app_id for g in games} == {"fortnite", "rocketleague"}

    async def test_read_all_as_empty(self, populated_pool: Pool):
        games = await populated_pool.read_all_as(
            GameRow,
            "SELECT id, platform, app_id, title FROM games WHERE platform = ?",
            ("nope",),
        )
        assert games == []


# ----------------------------------------------------------------------
# 5. Read transaction context
# ----------------------------------------------------------------------


class TestReadTransaction:
    async def test_read_transaction_runs_multiple_statements(self, populated_pool: Pool):
        async with populated_pool.read_transaction() as tx:
            games = await tx.read_all("SELECT id FROM games ORDER BY id")
            manifests = await tx.read_all("SELECT game_id FROM manifests")
        assert len(games) == 5
        assert len(manifests) == 3

    async def test_read_transaction_supports_dataclass_mapping(self, populated_pool: Pool):
        async with populated_pool.read_transaction() as tx:
            game = await tx.read_one_as(
                GameRow,
                "SELECT id, platform, app_id, title FROM games WHERE id = ?",
                (1,),
            )
        assert game.app_id == "10"

    async def test_read_transaction_supports_streaming(self, populated_pool: Pool):
        ids = []
        async with populated_pool.read_transaction() as tx:
            async for row in tx.read_stream("SELECT id FROM games ORDER BY id"):
                ids.append(row["id"])
        assert ids == [1, 2, 3, 4, 5]


# ----------------------------------------------------------------------
# 6. Write transaction context
# ----------------------------------------------------------------------


class TestWriteTransaction:
    async def test_write_transaction_commits_on_success(self, pool: Pool):
        async with pool.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', 'tx-1', 'TX Game', 1, 'never_prefilled')"
            )
        rows = await pool.read_all("SELECT title FROM games WHERE app_id = 'tx-1'")
        assert rows[0]["title"] == "TX Game"

    async def test_write_transaction_rolls_back_on_exception(self, pool: Pool):
        with pytest.raises(RuntimeError, match="rollback me"):
            async with pool.write_transaction() as tx:
                await tx.execute(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES ('steam', 'rb-1', 'Will Rollback', 1, 'never_prefilled')"
                )
                raise RuntimeError("rollback me")
        # Verify the insert was rolled back
        rows = await pool.read_all("SELECT title FROM games WHERE app_id = 'rb-1'")
        assert rows == []

    async def test_write_transaction_rolls_back_on_integrity_error(self, populated_pool: Pool):
        # populated_pool already has steam/10
        with pytest.raises(IntegrityViolationError):
            async with populated_pool.write_transaction() as tx:
                await tx.execute(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES ('steam', '440-other', 'Other', 1, 'never_prefilled')"
                )  # this would succeed if alone
                await tx.execute(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES ('steam', '10', 'Dupe', 1, 'never_prefilled')"
                )  # this fails on UNIQUE
        # Both inserts rolled back
        rows = await populated_pool.read_all(
            "SELECT title FROM games "
            "WHERE app_id IN ('440-other', '10') "
            "AND title NOT IN ('Counter-Strike', 'Team Fortress 2')"
        )
        assert rows == []

    async def test_write_transaction_supports_reads(self, populated_pool: Pool):
        async with populated_pool.write_transaction() as tx:
            count_before = (await tx.read_one("SELECT COUNT(*) AS c FROM games"))["c"]
            await tx.execute(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', 'mid-tx-1', 'Mid TX', 1, 'never_prefilled')"
            )
            count_after = (await tx.read_one("SELECT COUNT(*) AS c FROM games"))["c"]
            assert count_after == count_before + 1

    async def test_write_transaction_serializes(self, pool: Pool):
        """Two concurrent write transactions must serialize (writer lock)."""
        order: list[int] = []

        async def worker(worker_id: int) -> None:
            async with pool.write_transaction() as tx:
                order.append(worker_id)
                await asyncio.sleep(0.05)
                await tx.execute(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES (?, ?, ?, 1, 'never_prefilled')",
                    ("steam", f"ser-{worker_id}", f"W{worker_id}"),
                )
                order.append(worker_id)

        await asyncio.gather(worker(1), worker(2))
        # The lock guarantees: a worker's two appends are adjacent
        assert order in ([1, 1, 2, 2], [2, 2, 1, 1])


# ----------------------------------------------------------------------
# 7. Raw connection escape hatches
# ----------------------------------------------------------------------


class TestRawAcquire:
    async def test_acquire_reader_yields_aiosqlite_connection(self, populated_pool: Pool):
        import aiosqlite as _aiosqlite

        async with populated_pool.acquire_reader() as conn:
            assert isinstance(conn, _aiosqlite.Connection)
            async with conn.execute("SELECT COUNT(*) FROM games") as cur:
                row = await cur.fetchone()
                assert row[0] == 5

    async def test_acquire_reader_query_only_blocks_writes(self, pool: Pool):
        import aiosqlite as _aiosqlite

        async with pool.acquire_reader() as conn:
            with pytest.raises(_aiosqlite.OperationalError, match="readonly"):
                await conn.execute(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES ('steam', 'qo-1', 'Should Fail', 1, 'never_prefilled')"
                )

    async def test_acquire_writer_yields_aiosqlite_connection(self, pool: Pool):
        import aiosqlite as _aiosqlite

        async with pool.acquire_writer() as conn:
            assert isinstance(conn, _aiosqlite.Connection)
            await conn.execute(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', 'aw-1', 'Raw Writer', 1, 'never_prefilled')"
            )
            await conn.commit()
        rows = await pool.read_all("SELECT title FROM games WHERE app_id = 'aw-1'")
        assert rows[0]["title"] == "Raw Writer"

    async def test_acquire_writer_holds_lock_for_duration(self, pool: Pool):
        """While one caller holds acquire_writer, another can't enter."""
        entered = asyncio.Event()
        release = asyncio.Event()
        secondary_entered: list[float] = []

        async def hold_writer():
            async with pool.acquire_writer():
                entered.set()
                await release.wait()

        async def secondary():
            await entered.wait()
            t0 = asyncio.get_event_loop().time()
            async with pool.acquire_writer():
                secondary_entered.append(asyncio.get_event_loop().time() - t0)

        primary_task = asyncio.create_task(hold_writer())
        secondary_task = asyncio.create_task(secondary())

        await entered.wait()
        await asyncio.sleep(0.1)  # secondary should be blocked
        assert not secondary_entered  # hasn't entered yet
        release.set()
        await asyncio.gather(primary_task, secondary_task)
        assert secondary_entered  # entered after primary released
        assert secondary_entered[0] >= 0.1  # waited at least 100ms


# ----------------------------------------------------------------------
# 8. Error wrapping
# ----------------------------------------------------------------------


class TestErrorWrapping:
    async def test_unique_constraint_raises_integrity_violation(self, populated_pool: Pool):
        with pytest.raises(IntegrityViolationError) as exc_info:
            await populated_pool.execute_write(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', '10', 'Dupe', 1, 'never_prefilled')"
            )
        assert exc_info.value.constraint_kind == "unique"
        assert exc_info.value.table == "games"

    async def test_not_null_constraint_raises_integrity_violation(self, pool: Pool):
        with pytest.raises(IntegrityViolationError) as exc_info:
            await pool.execute_write(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('steam', 'nn-1', NULL, 1, 'never_prefilled')"
            )
        assert exc_info.value.constraint_kind == "notnull"

    async def test_foreign_key_constraint_raises_integrity_violation(self, pool: Pool):
        with pytest.raises(IntegrityViolationError) as exc_info:
            await pool.execute_write(
                "INSERT INTO manifests (game_id, raw, fetched_at) "
                "VALUES (9999, 'bytes', '2026-04-25T00:00:00Z')"
            )
        assert exc_info.value.constraint_kind == "fk"

    async def test_check_constraint_raises_integrity_violation(self, pool: Pool):
        with pytest.raises(IntegrityViolationError) as exc_info:
            await pool.execute_write(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES ('badplatform', 'x', 'X', 1, 'never_prefilled')"
            )
        assert exc_info.value.constraint_kind == "check"

    async def test_query_syntax_error_raises_query_syntax_error(self, pool: Pool):
        with pytest.raises(QuerySyntaxError):
            await pool.read_one("SELEKT * FROM games")

    async def test_no_such_table_raises_query_syntax_error(self, pool: Pool):
        with pytest.raises(QuerySyntaxError):
            await pool.read_one("SELECT * FROM not_a_real_table")

    async def test_integrity_error_log_does_not_leak_raw_params(self, populated_pool: Pool, capsys):
        from orchestrator.core import logging as log_mod

        log_mod.configure_logging()

        secret_value = "PARAM_NEVER_LEAK_SECRET"  # noqa: S105
        with contextlib.suppress(IntegrityViolationError):
            await populated_pool.execute_write(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES (?, ?, ?, 1, 'never_prefilled')",
                ("steam", "10", secret_value),  # platform/app_id collide → IntegrityError
            )

        out = capsys.readouterr().out
        assert secret_value not in out, f"raw param leaked to log: {out}"

    async def test_query_failed_log_does_not_leak_raw_sql_literals(self, pool: Pool, capsys):
        from orchestrator.core import logging as log_mod

        log_mod.configure_logging()

        secret_literal = "SECRET_IN_LITERAL_NOT_PARAM"  # noqa: S105
        with contextlib.suppress(QuerySyntaxError):
            # Hand-built SQL with a literal — not a parameter — to verify
            # _template_only strips literals from log output.
            await pool.read_one(f"SELEKT * WHERE x = '{secret_literal}'")

        out = capsys.readouterr().out
        assert secret_literal not in out, f"raw literal leaked to log: {out}"


# ----------------------------------------------------------------------
# 9. Module-level singleton
# ----------------------------------------------------------------------


class TestModuleSingleton:
    async def test_get_pool_before_init_raises(self, reset_singleton):
        with pytest.raises(PoolNotInitializedError):
            get_pool()

    async def test_init_pool_then_get_pool_returns_singleton(
        self, reset_singleton, monkeypatch, db_path: Path
    ):
        # Patch Settings to point at our test db_path
        from orchestrator.core.settings import Settings

        monkeypatch.setattr(
            Settings,
            "model_config",
            {**Settings.model_config, "secrets_dir": None},
        )
        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        # Also need to clear get_settings cache so the new env var takes
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()

        pool_a = await init_pool()
        pool_b = get_pool()
        assert pool_a is pool_b
        await close_pool()

    async def test_init_pool_idempotent(self, reset_singleton, monkeypatch, db_path: Path):
        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()

        pool_a = await init_pool()
        pool_b = await init_pool()
        assert pool_a is pool_b
        await close_pool()

    async def test_reload_pool_returns_fresh_instance(
        self, reset_singleton, monkeypatch, db_path: Path
    ):
        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()

        pool_a = await init_pool()
        pool_b = await reload_pool()
        assert pool_a is not pool_b
        await close_pool()

    async def test_close_pool_when_uninitialized_is_noop(self, reset_singleton):
        await close_pool()  # must not raise
        with pytest.raises(PoolNotInitializedError):
            get_pool()

    async def test_use_after_close_pool_raises_uninitialized(
        self, reset_singleton, monkeypatch, db_path: Path
    ):
        monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()

        await init_pool()
        await close_pool()
        with pytest.raises(PoolNotInitializedError):
            get_pool()
