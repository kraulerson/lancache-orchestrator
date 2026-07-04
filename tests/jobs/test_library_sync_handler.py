"""Tests for orchestrator.jobs.handlers.library_sync (BL11 / re-arch ③b).

Steam library enumeration is sourced from the data-plane agent's prefilled
apps (the manifest .bin cache) and each app is classified via the public Steam
store appdetails API; only type=='game' is upserted. Epic dispatch is covered in
test_epic_handlers.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.jobs.handlers.library_sync import library_sync_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


def _job(platform: str = "steam") -> dict[str, Any]:
    return {
        "id": 1,
        "kind": "library_sync",
        "platform": platform,
        "game_id": None,
        "payload": None,
    }


async def test_rejects_unsupported_platform(pool):
    # steam + epic are supported; an unknown platform rejects before any work.
    with pytest.raises(ValueError, match="unsupported platform"):
        await library_sync_handler(_job(platform="gog"), Deps(pool=pool))


async def test_requires_agent_client(pool):
    with pytest.raises(RuntimeError, match="agent_client is required"):
        await library_sync_handler(_job(), Deps(pool=pool, agent_client=None))


class _StubAgent:
    def __init__(self, app_ids):
        self._app_ids = app_ids

    async def prefilled_apps(self):
        return self._app_ids


class TestEnumerateViaPrefill:
    """re-arch ③b: the prefill enumeration looks up each prefilled app via the
    public Steam store appdetails API to get its type + name, upserts ONLY
    type=='game' (with the real name), caches results in steam_app_info, and is
    bounded per run by steam_store_fetch_budget (filling over later syncs)."""

    def _patch_settings(self, monkeypatch, *, budget=150, delay=0.0):
        from orchestrator.core import settings as settings_mod

        monkeypatch.setattr(
            "orchestrator.jobs.handlers.library_sync.get_settings",
            lambda: settings_mod.Settings(
                orchestrator_token="a" * 32,
                steam_store_fetch_budget=budget,
                steam_store_fetch_delay_sec=delay,
            ),
        )

    async def test_caches_and_upserts_games(self, pool, monkeypatch):
        self._patch_settings(monkeypatch)

        async def fake_fetch(app_id: int):
            if app_id == 440:
                return {
                    "type": "game",
                    "name": "Team Fortress 2",
                    "has_single_player": 0,
                    "has_multiplayer": 1,
                }
            return {
                "type": "dlc",
                "name": "Some DLC",
                "has_single_player": None,
                "has_multiplayer": None,
            }

        monkeypatch.setattr("orchestrator.jobs.handlers.library_sync.fetch_app_info", fake_fetch)
        agent = _StubAgent([440, 570])
        await library_sync_handler(_job(), Deps(pool=pool, agent_client=agent))

        games = await pool.read_all(
            "SELECT app_id, title, owned FROM games WHERE platform='steam' ORDER BY app_id"
        )
        # Only the game (440) was inserted; the dlc (570) was filtered out.
        assert [(r["app_id"], r["title"], r["owned"]) for r in games] == [
            ("440", "Team Fortress 2", 1)
        ]
        # Both lookups were cached (so they aren't re-fetched next run).
        cache = await pool.read_all(
            "SELECT app_id, app_type, name FROM steam_app_info ORDER BY app_id"
        )
        assert {(r["app_id"], r["app_type"]) for r in cache} == {("440", "game"), ("570", "dlc")}

    async def test_uses_cache_no_refetch(self, pool, monkeypatch):
        self._patch_settings(monkeypatch)
        # Fully cached = category flags already present, so no re-fetch (MP-only #366).
        await pool.execute_write(
            "INSERT INTO steam_app_info "
            "(app_id, app_type, name, has_single_player, has_multiplayer) "
            "VALUES ('440','game','Team Fortress 2', 0, 1)"
        )

        async def boom(app_id: int):
            raise AssertionError("fetch_app_info must not be called on a cache hit")

        monkeypatch.setattr("orchestrator.jobs.handlers.library_sync.fetch_app_info", boom)
        agent = _StubAgent([440])
        await library_sync_handler(_job(), Deps(pool=pool, agent_client=agent))

        row = await pool.read_one("SELECT title, owned FROM games WHERE app_id='440'")
        assert row["title"] == "Team Fortress 2"
        assert row["owned"] == 1

    async def test_backfills_null_category_flags(self, pool, monkeypatch):
        # A row cached before categories were tracked (flags NULL) is re-fetched
        # once to backfill them, then classify can see it (MP-only #366).
        self._patch_settings(monkeypatch)
        await pool.execute_write(
            "INSERT INTO steam_app_info (app_id, app_type, name) VALUES ('570','game','Dota 2')"
        )

        async def fake_fetch(app_id: int):
            return {
                "type": "game",
                "name": "Dota 2",
                "has_single_player": 0,
                "has_multiplayer": 1,
            }

        monkeypatch.setattr("orchestrator.jobs.handlers.library_sync.fetch_app_info", fake_fetch)
        agent = _StubAgent([570])
        await library_sync_handler(_job(), Deps(pool=pool, agent_client=agent))

        row = await pool.read_one(
            "SELECT has_single_player, has_multiplayer FROM steam_app_info WHERE app_id='570'"
        )
        assert row["has_single_player"] == 0
        assert row["has_multiplayer"] == 1

    async def test_budget_limits_fetches(self, pool, monkeypatch):
        self._patch_settings(monkeypatch, budget=1)
        calls: list[int] = []

        async def counting_fetch(app_id: int):
            calls.append(app_id)
            return {
                "type": "game",
                "name": f"Game {app_id}",
                "has_single_player": 1,
                "has_multiplayer": 0,
            }

        monkeypatch.setattr(
            "orchestrator.jobs.handlers.library_sync.fetch_app_info", counting_fetch
        )
        agent = _StubAgent([1, 2, 3])
        await library_sync_handler(_job(), Deps(pool=pool, agent_client=agent))

        # Budget=1 → only one uncached app is looked up this run; the rest defer.
        assert len(calls) == 1

    async def test_existing_title_updated_to_store_name(self, pool, monkeypatch):
        self._patch_settings(monkeypatch)
        await pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned) VALUES ('steam','440','Old Name',0)"
        )

        async def fake_fetch(app_id: int):
            return {
                "type": "game",
                "name": "Team Fortress 2",
                "has_single_player": 0,
                "has_multiplayer": 1,
            }

        monkeypatch.setattr("orchestrator.jobs.handlers.library_sync.fetch_app_info", fake_fetch)
        agent = _StubAgent([440])
        await library_sync_handler(_job(), Deps(pool=pool, agent_client=agent))

        row = await pool.read_one("SELECT title, owned FROM games WHERE app_id='440'")
        assert row["title"] == "Team Fortress 2"  # overwritten with the store name
        assert row["owned"] == 1
