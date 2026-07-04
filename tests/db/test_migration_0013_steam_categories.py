"""Migration 0013 — steam_app_info gains has_single_player / has_multiplayer (MP-only, #366).

The columns back the classifier's multiplayer-only detection. They are nullable
(NULL = categories not yet fetched); a row inserted without them defaults to NULL,
and existing rows are unaffected by the ADD COLUMN.
"""

from __future__ import annotations

import pytest

from orchestrator.db.pool import PoolError


async def test_category_columns_accept_values(pool) -> None:
    await pool.execute_write(
        "INSERT INTO steam_app_info (app_id, app_type, name, has_single_player, has_multiplayer) "
        "VALUES ('570', 'game', 'Dota 2', 0, 1)"
    )
    rows = await pool.read_all(
        "SELECT has_single_player, has_multiplayer FROM steam_app_info WHERE app_id = '570'"
    )
    assert rows[0]["has_single_player"] == 0
    assert rows[0]["has_multiplayer"] == 1


async def test_row_without_flags_defaults_null(pool) -> None:
    # The pre-0013 insert shape (no category columns) still works; flags are NULL.
    await pool.execute_write(
        "INSERT INTO steam_app_info (app_id, app_type, name) VALUES ('730', 'game', 'CS2')"
    )
    rows = await pool.read_all(
        "SELECT has_single_player, has_multiplayer FROM steam_app_info WHERE app_id = '730'"
    )
    assert rows[0]["has_single_player"] is None
    assert rows[0]["has_multiplayer"] is None


async def test_flags_are_integer_typed(pool) -> None:
    # STRICT table: a non-integer flag is rejected.
    with pytest.raises(PoolError):
        await pool.execute_write(
            "INSERT INTO steam_app_info (app_id, app_type, name, has_multiplayer) "
            "VALUES ('1', 'game', 'X', 'notanint')"
        )
