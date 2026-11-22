"""New - Not Cached: check-set and overdue-set selection (spec section 3)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from orchestrator.clients import heartbeat
from orchestrator.jobs.new_games import (
    OverdueGame,
    overdue_message,
    select_check_set,
    select_overdue,
)


def _ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _game(
    pool, app_id, *, status="unknown", seen=None, owned=1, platform="steam", title=None
):
    # Test setup only: seeds status directly (writer guard scans src/ only).
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, first_seen_at) "
        "VALUES (?,?,?,?,?,?)",
        (platform, app_id, title or f"T{app_id}", owned, status, seen),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


async def test_check_set_includes_unknown_not_downloaded_partial_within_window(pool):
    a = await _game(pool, "1", status="unknown", seen=_ago(days=1))
    b = await _game(pool, "2", status="not_downloaded", seen=_ago(days=2))
    c = await _game(pool, "3", status="validation_failed", seen=_ago(days=3), platform="epic")
    assert await select_check_set(pool, window_days=7) == [c, b, a]


async def test_check_set_excludes_up_to_date_pending_update_outside_window_null_first_seen(pool):
    await _game(pool, "1", status="up_to_date", seen=_ago(days=1))
    await _game(pool, "2", status="pending_update", seen=_ago(days=1))
    await _game(pool, "3", status="unknown", seen=_ago(days=8))
    await _game(pool, "4", status="unknown", seen=None)
    assert await select_check_set(pool, window_days=7) == []


async def test_check_set_excludes_blocked_and_excluded(pool):
    await _game(pool, "1", seen=_ago(days=1))
    await _game(pool, "2", seen=_ago(days=1))
    keep = await _game(pool, "3", seen=_ago(days=1))
    other = await _game(pool, "4", seen=_ago(days=1))
    await pool.execute_write("INSERT INTO block_list (platform, app_id) VALUES ('steam','1')")
    await pool.execute_write(
        "INSERT INTO prefill_exclusions (platform, app_id, mode) VALUES ('steam','2','exclude')"
    )
    await pool.execute_write(
        "INSERT INTO prefill_exclusions (platform, app_id, mode) VALUES ('steam','4','allow')"
    )
    assert sorted(await select_check_set(pool, window_days=7)) == sorted([keep, other])


async def test_check_set_excludes_unowned(pool):
    await _game(pool, "1", seen=_ago(days=1), owned=0)
    assert await select_check_set(pool, window_days=7) == []


async def test_window_boundary(pool):
    inside = await _game(pool, "1", seen=_ago(days=6, hours=23))
    await _game(pool, "2", seen=_ago(days=7, hours=1))
    assert await select_check_set(pool, window_days=7) == [inside]


async def test_overdue_not_cached_after_window(pool):
    g = await _game(pool, "1", status="not_downloaded", seen=_ago(days=8), title="Alpha")
    await _game(pool, "2", status="pending_update", seen=_ago(days=8))
    await _game(pool, "3", status="up_to_date", seen=_ago(days=8))
    out = await select_overdue(pool, window_days=7, unknown_hours=48)
    assert out == [OverdueGame(g, "Alpha", "not cached")]


async def test_overdue_unknown_after_48h(pool):
    g = await _game(pool, "1", status="unknown", seen=_ago(hours=49), title="Beta")
    await _game(pool, "2", status="unknown", seen=_ago(hours=47))
    out = await select_overdue(pool, window_days=7, unknown_hours=48)
    assert out == [OverdueGame(g, "Beta", "unmeasured")]


async def test_overdue_both_reasons_reported_once(pool):
    g = await _game(pool, "1", status="unknown", seen=_ago(days=9), title="Both")
    out = await select_overdue(pool, window_days=7, unknown_hours=48)
    assert out == [OverdueGame(g, "Both", "not cached")]


async def test_overdue_never_includes_null_first_seen_blocked_or_excluded(pool):
    await _game(pool, "1", status="unknown", seen=None)
    await _game(pool, "2", status="unknown", seen=_ago(days=9))
    await _game(pool, "3", status="unknown", seen=_ago(days=9))
    await _game(pool, "4", status="unknown", seen=_ago(days=9), owned=0)
    # mode='allow' is NOT an exclusion: this game must still be reported.
    allowed = await _game(pool, "5", status="unknown", seen=_ago(days=9), title="Allowed")
    await pool.execute_write("INSERT INTO block_list (platform, app_id) VALUES ('steam','2')")
    await pool.execute_write(
        "INSERT INTO prefill_exclusions (platform, app_id, mode) VALUES ('steam','3','exclude')"
    )
    await pool.execute_write(
        "INSERT INTO prefill_exclusions (platform, app_id, mode) VALUES ('steam','5','allow')"
    )
    assert await select_overdue(pool, window_days=7, unknown_hours=48) == [
        OverdueGame(allowed, "Allowed", "not cached")
    ]


def test_overdue_message_empty():
    assert overdue_message([]) == "no new games overdue"


def test_overdue_message_truncates_to_push_limit_with_more_count():
    games = [
        OverdueGame(i, f"A Fairly Long Game Title Number {i}", "not cached") for i in range(40)
    ]
    msg = overdue_message(games)
    assert len(msg) <= heartbeat.MSG_MAX_CHARS
    m = re.search(r"\(\+(\d+) more\)$", msg)
    assert m
    shown = msg.count("(not cached)")
    assert int(m.group(1)) == 40 - shown
    assert msg.startswith("overdue 40: ")


def test_overdue_message_short_list_untruncated():
    msg = overdue_message([OverdueGame(1, "A", "not cached"), OverdueGame(2, "B", "unmeasured")])
    assert msg == "overdue 2: A (not cached), B (unmeasured)"
