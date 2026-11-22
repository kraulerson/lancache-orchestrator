"""first_seen_at is stamped on INSERT only by library sync (New - Not Cached)."""

from __future__ import annotations

import re

import pytest

from orchestrator.core import settings as settings_mod
from orchestrator.jobs.handlers.library_sync import library_sync_handler
from orchestrator.jobs.worker import Deps
from orchestrator.platform.epic.models import EpicLibraryItem

pytestmark = pytest.mark.asyncio

_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class _StubAgent:
    def __init__(self, app_ids):
        self._app_ids = app_ids

    async def prefilled_apps(self):
        return self._app_ids


class _StubEpic:
    def __init__(self, items):
        self._items = items

    async def library_enumerate(self):
        return self._items


def _job(platform: str) -> dict:
    return {"id": 1, "kind": "library_sync", "platform": platform, "game_id": None}


@pytest.fixture
def steam_env(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.jobs.handlers.library_sync.get_settings",
        lambda: settings_mod.Settings(
            orchestrator_token="a" * 32,
            steam_store_fetch_budget=150,
            steam_store_fetch_delay_sec=0.0,
        ),
    )

    async def fake_fetch(app_id: int):
        return {"type": "game", "name": "TF2", "has_single_player": 0, "has_multiplayer": 1}

    monkeypatch.setattr("orchestrator.jobs.handlers.library_sync.fetch_app_info", fake_fetch)


async def _steam_sync(pool):
    await library_sync_handler(_job("steam"), Deps(pool=pool, agent_client=_StubAgent([440])))


async def _epic_sync(pool):
    stub = _StubEpic([EpicLibraryItem("AppA", "ns", "cat", "Game A")])
    await library_sync_handler(_job("epic"), Deps(pool=pool, epic_client=stub))


async def _first_seen(pool, platform):
    row = await pool.read_one("SELECT first_seen_at FROM games WHERE platform=?", (platform,))
    return row["first_seen_at"]


async def test_new_steam_row_gets_first_seen_at(pool, steam_env):
    await _steam_sync(pool)
    assert _TS.match(await _first_seen(pool, "steam"))


async def test_new_epic_row_gets_first_seen_at(pool):
    await _epic_sync(pool)
    assert _TS.match(await _first_seen(pool, "epic"))


async def test_resync_does_not_change_first_seen_at(pool, steam_env):
    await _steam_sync(pool)
    await _epic_sync(pool)
    await pool.execute_write("UPDATE games SET first_seen_at='2026-01-01T00:00:00Z'")
    await _steam_sync(pool)
    await _epic_sync(pool)
    assert await _first_seen(pool, "steam") == "2026-01-01T00:00:00Z"
    assert await _first_seen(pool, "epic") == "2026-01-01T00:00:00Z"


async def test_existing_null_row_stays_null_on_resync(pool, steam_env):
    # A pre-0019 row has NULL first_seen_at; it must never become "new".
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) VALUES ('steam','440','Old',1)"
    )
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, metadata) "
        "VALUES ('epic','AppA','Old',1,'{}')"
    )
    await _steam_sync(pool)
    await _epic_sync(pool)
    assert await _first_seen(pool, "steam") is None
    assert await _first_seen(pool, "epic") is None
