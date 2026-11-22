"""Feature "New - Not Cached": games API exposes first_seen_at and is_new."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.asyncio

AUTH = {"Authorization": f"Bearer {'a' * 32}"}


def _ts(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _set_first_seen(pool, value: str | None, game_id: int = 1) -> None:
    await pool.execute_write("UPDATE games SET first_seen_at=? WHERE id=?", (value, game_id))


async def _detail(client, game_id: int = 1) -> dict:
    r = await client.get(f"/api/v1/games/{game_id}", headers=AUTH)
    assert r.status_code == 200
    return r.json()["game"]


async def test_is_new_true_within_window(client, populated_pool) -> None:
    await _set_first_seen(populated_pool, _ts(1))
    assert (await _detail(client))["is_new"] is True


async def test_is_new_false_at_7d_plus(client, populated_pool) -> None:
    await _set_first_seen(populated_pool, _ts(7.01))
    assert (await _detail(client))["is_new"] is False


async def test_is_new_false_when_first_seen_null(client, populated_pool) -> None:
    await _set_first_seen(populated_pool, None)
    g = await _detail(client)
    assert g["is_new"] is False
    assert g["first_seen_at"] is None


async def test_is_new_false_when_blocked(client, populated_pool) -> None:
    await _set_first_seen(populated_pool, _ts(1))
    g = await _detail(client)
    await populated_pool.execute_write(
        "INSERT INTO block_list (platform, app_id, source) VALUES (?, ?, 'api')",
        (g["platform"], g["app_id"]),
    )
    assert (await _detail(client))["is_new"] is False


async def test_is_new_false_when_excluded_but_not_when_allow(client, populated_pool) -> None:
    await _set_first_seen(populated_pool, _ts(1))
    g = await _detail(client)
    await populated_pool.execute_write(
        "INSERT INTO prefill_exclusions (platform, app_id, mode) VALUES (?, ?, 'allow')",
        (g["platform"], g["app_id"]),
    )
    assert (await _detail(client))["is_new"] is True
    await populated_pool.execute_write(
        "UPDATE prefill_exclusions SET mode='exclude' WHERE platform=? AND app_id=?",
        (g["platform"], g["app_id"]),
    )
    g2 = await _detail(client)
    assert g2["is_new"] is False
    assert "excluded" not in g2


async def test_first_seen_at_in_list_and_detail(client, populated_pool) -> None:
    stamp = _ts(2)
    await _set_first_seen(populated_pool, stamp)
    r = await client.get("/api/v1/games", headers=AUTH)
    row = next(g for g in r.json()["games"] if g["id"] == 1)
    assert row["first_seen_at"] == stamp
    assert row["is_new"] is True
    assert (await _detail(client))["first_seen_at"] == stamp


async def test_malformed_first_seen_at_renders_is_new_false_not_500(client, populated_pool) -> None:
    await _set_first_seen(populated_pool, "not-a-date")
    r = await client.get("/api/v1/games", headers=AUTH)
    assert r.status_code == 200
    row = next(g for g in r.json()["games"] if g["id"] == 1)
    assert row["is_new"] is False
    assert row["first_seen_at"] == "not-a-date"
    assert (await _detail(client))["is_new"] is False


async def test_is_new_exact_window_boundary_matches_sql_second_truncation(
    client, populated_pool, monkeypatch
) -> None:
    from orchestrator.api.routers import games as games_mod

    # Non-zero microsecond: SQL truncates 'now' to whole seconds, so must the API.
    now = datetime(2026, 11, 21, 12, 0, 0, 500_000, tzinfo=UTC)
    monkeypatch.setattr(games_mod, "_utcnow", lambda: now)
    edge = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _set_first_seen(populated_pool, edge)
    assert (await _detail(client))["is_new"] is True
    before = (now - timedelta(days=7, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _set_first_seen(populated_pool, before)
    assert (await _detail(client))["is_new"] is False
