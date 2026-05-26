"""Tests for orchestrator.jobs.handlers.library_sync (BL11)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from orchestrator.jobs.handlers.library_sync import library_sync_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


class _StubSteam:
    """Minimal stand-in for SteamWorkerClient — only `library_enumerate()`
    is exercised by the handler."""

    def __init__(
        self,
        result: dict[str, Any] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._result = result or {"apps": []}
        self._raises = raises
        self.calls = 0

    async def library_enumerate(self) -> dict[str, Any]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._result


def _job(platform: str = "steam") -> dict[str, Any]:
    return {
        "id": 1,
        "kind": "library_sync",
        "platform": platform,
        "game_id": None,
        "payload": None,
    }


class TestUpsertHappyPath:
    async def test_upserts_owned_games(self, pool):
        stub = _StubSteam(
            result={
                "apps": [
                    {"app_id": 730, "name": "Counter-Strike 2", "depots": [731, 734]},
                    {"app_id": 440, "name": "Team Fortress 2", "depots": []},
                ]
            }
        )
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))

        rows = await pool.read_all(
            "SELECT platform, app_id, title, owned, metadata FROM games ORDER BY app_id"
        )
        assert len(rows) == 2
        cs2 = next(r for r in rows if r["app_id"] == "730")
        assert cs2["title"] == "Counter-Strike 2"
        assert cs2["owned"] == 1
        md = json.loads(cs2["metadata"])
        assert md["depots"] == [731, 734]
        assert md["steam_packages"] == []

    async def test_empty_library_zero_inserts(self, pool):
        stub = _StubSteam(result={"apps": []})
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))
        rows = await pool.read_all("SELECT id FROM games")
        assert rows == []

    async def test_skips_apps_with_missing_id(self, pool):
        stub = _StubSteam(
            result={
                "apps": [
                    {"app_id": 730, "name": "CS2", "depots": []},
                    {"name": "no app_id here", "depots": []},  # bad
                    {"app_id": 440, "name": "TF2", "depots": []},
                ]
            }
        )
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))
        rows = await pool.read_all("SELECT app_id FROM games ORDER BY app_id")
        assert {r["app_id"] for r in rows} == {"730", "440"}

    async def test_skips_apps_with_missing_name(self, pool):
        stub = _StubSteam(
            result={
                "apps": [
                    {"app_id": 730, "name": "", "depots": []},  # empty
                    {"app_id": 440, "name": "TF2", "depots": []},
                ]
            }
        )
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))
        rows = await pool.read_all("SELECT app_id FROM games")
        assert [r["app_id"] for r in rows] == ["440"]

    async def test_converts_int_app_id_to_string(self, pool):
        stub = _StubSteam(result={"apps": [{"app_id": 999, "name": "x", "depots": []}]})
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))
        row = await pool.read_one("SELECT app_id FROM games LIMIT 1")
        assert row["app_id"] == "999"
        assert isinstance(row["app_id"], str)


class TestIdempotency:
    async def test_re_sync_no_duplicate_rows(self, pool):
        stub = _StubSteam(result={"apps": [{"app_id": 730, "name": "CS2", "depots": [731]}]})
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))
        rows = await pool.read_all("SELECT app_id FROM games")
        assert len(rows) == 1

    async def test_re_sync_updates_title_and_metadata(self, pool):
        stub_v1 = _StubSteam(
            result={
                "apps": [
                    {
                        "app_id": 730,
                        "name": "Counter-Strike: Global Offensive",
                        "depots": [731],
                    }
                ]
            }
        )
        stub_v2 = _StubSteam(
            result={
                "apps": [
                    {
                        "app_id": 730,
                        "name": "Counter-Strike 2",
                        "depots": [731, 734],
                    }
                ]
            }
        )

        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub_v1))
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub_v2))

        row = await pool.read_one("SELECT title, metadata FROM games WHERE app_id=?", ("730",))
        assert row["title"] == "Counter-Strike 2"
        assert json.loads(row["metadata"])["depots"] == [731, 734]

    async def test_re_sync_preserves_status_cached_version_etc(self, pool):
        # Pre-seed a game with downstream state populated.
        await pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned, status, "
            "cached_version, current_version, last_validated_at) "
            "VALUES (?, ?, ?, 1, 'up_to_date', 'v1', 'v1', '2026-05-01T00:00:00Z')",
            ("steam", "730", "CS:GO"),
        )
        stub = _StubSteam(
            result={"apps": [{"app_id": 730, "name": "Counter-Strike 2", "depots": []}]}
        )
        await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))

        row = await pool.read_one(
            "SELECT title, status, cached_version, current_version, last_validated_at "
            "FROM games WHERE app_id=?",
            ("730",),
        )
        assert row["title"] == "Counter-Strike 2"
        assert row["status"] == "up_to_date"
        assert row["cached_version"] == "v1"
        assert row["current_version"] == "v1"
        assert row["last_validated_at"] == "2026-05-01T00:00:00Z"


class TestErrorPropagation:
    async def test_rejects_non_steam_platform(self, pool):
        stub = _StubSteam()
        with pytest.raises(ValueError, match="library_sync only supports steam"):
            await library_sync_handler(_job(platform="epic"), Deps(pool=pool, steam_client=stub))
        assert stub.calls == 0  # didn't even ask the worker

    async def test_requires_steam_client(self, pool):
        with pytest.raises(RuntimeError, match="steam_client is required"):
            await library_sync_handler(_job(), Deps(pool=pool, steam_client=None))

    async def test_steam_worker_error_propagates(self, pool):
        from orchestrator.platform.steam.client import SteamWorkerError

        stub = _StubSteam(raises=SteamWorkerError("NotAuthenticated", "no session"))
        # Migration seeds platforms(name='steam', auth_status='never'); set to 'ok'
        # so we can verify the handler flips it.
        await pool.execute_write("UPDATE platforms SET auth_status='ok' WHERE name='steam'")
        with pytest.raises(SteamWorkerError):
            await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))
        rows = await pool.read_all("SELECT id FROM games")
        assert rows == []  # no partial writes

    async def test_not_authenticated_flips_platform_auth_status_to_expired(self, pool):
        """F-UAT6-3: when the steam worker reports NotAuthenticated, the
        handler MUST update platforms.auth_status='expired' so the
        operator surfaces (`GET /platforms` and `GET /auth/status`) don't
        disagree. The SteamWorkerError still propagates so the job is
        marked failed."""
        from orchestrator.platform.steam.client import SteamWorkerError

        # Pre-seed the platforms row in 'ok' state (simulating a prior
        # successful auth that has now lapsed Steam-side).
        await pool.execute_write("UPDATE platforms SET auth_status='ok' WHERE name='steam'")
        stub = _StubSteam(raises=SteamWorkerError("NotAuthenticated", "lapsed"))

        with pytest.raises(SteamWorkerError):
            await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))

        row = await pool.read_one(
            "SELECT auth_status, last_error FROM platforms WHERE name='steam'"
        )
        assert row["auth_status"] == "expired", (
            f"expected platforms.auth_status='expired'; got {row['auth_status']!r}"
        )

    async def test_non_notauth_steam_error_does_not_flip_auth_status(self, pool):
        """Other SteamWorkerError kinds (e.g. SteamAPIError) must NOT
        flip auth_status — those represent transient API failures, not
        session expiry."""
        from orchestrator.platform.steam.client import SteamWorkerError

        await pool.execute_write("UPDATE platforms SET auth_status='ok' WHERE name='steam'")
        stub = _StubSteam(raises=SteamWorkerError("SteamAPIError", "transient blip"))

        with pytest.raises(SteamWorkerError):
            await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))

        row = await pool.read_one("SELECT auth_status FROM platforms WHERE name='steam'")
        assert row["auth_status"] == "ok"

    async def test_ipc_timeout_propagates_no_partial_writes(self, pool):
        from orchestrator.platform.steam.client import IPCTimeoutError

        stub = _StubSteam(raises=IPCTimeoutError("worker timeout"))
        with pytest.raises(IPCTimeoutError):
            await library_sync_handler(_job(), Deps(pool=pool, steam_client=stub))
        rows = await pool.read_all("SELECT id FROM games")
        assert rows == []
