"""Tests for orchestrator.jobs.handlers.manifest_fetch (BL12)."""

from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest

from orchestrator.jobs.handlers.manifest_fetch import manifest_fetch_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


class _StubSteam:
    """Minimal stand-in for SteamWorkerClient.manifest_fetch."""

    def __init__(self, *, result=None, raises=None):
        self._result = result or {"manifests": []}
        self._raises = raises
        self.calls: list[dict] = []

    async def manifest_fetch(self, app_id):
        self.calls.append({"app_id": app_id})
        if self._raises is not None:
            raise self._raises
        return self._result


async def _seed_game(pool, *, app_id="730", title="Counter-Strike 2"):
    """Insert a steam game; return its id."""
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, metadata) VALUES (?, ?, ?, 1, ?)",
        (
            "steam",
            app_id,
            title,
            json.dumps({"depots": [731, 734], "steam_packages": []}),
        ),
    )
    row = await pool.read_one("SELECT id FROM games WHERE app_id=?", (app_id,))
    return row["id"]


def _write_blob(raw_bytes: bytes) -> str:
    """Write a BLOB to a temp file (as the worker does) and return its path.

    S2-1: the worker hands manifest BLOBs to the handler via temp files on
    the shared FS, not base64 in the IPC line. The handler reads + unlinks.
    """
    path = os.path.join(tempfile.gettempdir(), f"test-manifest-{uuid.uuid4().hex}.zst")
    with open(path, "wb") as fh:
        fh.write(raw_bytes)
    return path


def _fake_manifest_payload(
    depot_id: int,
    gid: int,
    name: str,
    total_bytes: int,
    chunk_count: int,
    raw_bytes: bytes = b"FAKE_PROTOBUF",
) -> dict:
    """Build one entry of the worker's `manifest.fetch` IPC response."""
    return {
        "depot_id": depot_id,
        "manifest_gid": gid,
        "name": name,
        "total_bytes": total_bytes,
        "chunk_count": chunk_count,
        "raw_path": _write_blob(raw_bytes),
    }


def _job(game_id: int) -> dict:
    return {
        "id": 1,
        "kind": "manifest_fetch",
        "platform": "steam",
        "game_id": game_id,
        "payload": None,
    }


class TestHappyPath:
    async def test_inserts_one_manifest_row(self, pool):
        game_id = await _seed_game(pool)
        stub = _StubSteam(
            result={
                "manifests": [
                    _fake_manifest_payload(731, 1234567890123, "cs2-content", 5_000_000_000, 1820)
                ]
            }
        )
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))

        rows = await pool.read_all(
            "SELECT game_id, version, chunk_count, total_bytes, length(raw) AS raw_len "
            "FROM manifests"
        )
        assert len(rows) == 1
        assert rows[0]["game_id"] == game_id
        assert rows[0]["version"] == "1234567890123"  # gid as string
        assert rows[0]["chunk_count"] == 1820
        assert rows[0]["total_bytes"] == 5_000_000_000
        assert rows[0]["raw_len"] > 0

    async def test_stores_depot_id(self, pool):
        """F7 needs depot_id stored to build chunk URLs (migration 0003)."""
        game_id = await _seed_game(pool)
        stub = _StubSteam(result={"manifests": [_fake_manifest_payload(731, 100, "d1", 1000, 10)]})
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))
        row = await pool.read_one("SELECT depot_id FROM manifests WHERE game_id=?", (game_id,))
        assert row["depot_id"] == 731

    async def test_inserts_multiple_depot_manifests(self, pool):
        game_id = await _seed_game(pool)
        stub = _StubSteam(
            result={
                "manifests": [
                    _fake_manifest_payload(731, 100, "depot-731", 1_000, 10),
                    _fake_manifest_payload(734, 200, "depot-734", 2_000, 20),
                    _fake_manifest_payload(735, 300, "depot-735", 3_000, 30),
                ]
            }
        )
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))

        rows = await pool.read_all("SELECT version, total_bytes FROM manifests ORDER BY version")
        assert [r["version"] for r in rows] == ["100", "200", "300"]
        assert [r["total_bytes"] for r in rows] == [1_000, 2_000, 3_000]

    async def test_updates_games_size_bytes_to_sum(self, pool):
        """Per spec §6.3: games.size_bytes set to the sum of manifest
        total_bytes (full game install size across all depots)."""
        game_id = await _seed_game(pool)
        stub = _StubSteam(
            result={
                "manifests": [
                    _fake_manifest_payload(731, 1, "d1", 1_000, 10),
                    _fake_manifest_payload(734, 2, "d2", 2_500, 20),
                ]
            }
        )
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))

        row = await pool.read_one("SELECT size_bytes FROM games WHERE id=?", (game_id,))
        assert row["size_bytes"] == 3_500

    async def test_raw_blob_is_zstd_compressed_protobuf(self, pool):
        """The raw column stores zstd-compressed protobuf bytes. The
        handler is content-agnostic; it just decodes the base64 and
        stores. Worker is responsible for compress + encode."""
        game_id = await _seed_game(pool)
        # Note: even though we send fake bytes, the handler doesn't
        # try to decompress — it's an opaque BLOB at the orchestrator
        # layer (ADR-0013 D14).
        fake_raw = b"\x28\xb5\x2f\xfd\x00\x00stub-zstd-payload"
        stub = _StubSteam(
            result={
                "manifests": [
                    {
                        "depot_id": 731,
                        "manifest_gid": 42,
                        "name": "d1",
                        "total_bytes": 1000,
                        "chunk_count": 5,
                        "raw_path": _write_blob(fake_raw),
                    }
                ]
            }
        )
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))
        row = await pool.read_one("SELECT raw FROM manifests")
        assert row["raw"] == fake_raw


class TestIdempotency:
    async def test_re_fetch_same_gid_updates_existing_row(self, pool):
        """UNIQUE(game_id, version) — re-fetch upserts."""
        game_id = await _seed_game(pool)
        stub_v1 = _StubSteam(
            result={"manifests": [_fake_manifest_payload(731, 100, "v1", 1_000, 10)]}
        )
        stub_v2 = _StubSteam(
            result={"manifests": [_fake_manifest_payload(731, 100, "v1", 1_500, 15)]}
        )
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub_v1))
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub_v2))

        rows = await pool.read_all("SELECT version, total_bytes, chunk_count FROM manifests")
        assert len(rows) == 1  # upsert, no duplicate
        assert rows[0]["version"] == "100"
        assert rows[0]["total_bytes"] == 1_500
        assert rows[0]["chunk_count"] == 15

    async def test_re_fetch_new_gid_adds_row_preserves_old(self, pool):
        """New manifest_gid = different version row. Old version stays
        in the table (historical record)."""
        game_id = await _seed_game(pool)
        stub_v1 = _StubSteam(
            result={"manifests": [_fake_manifest_payload(731, 100, "v1", 1_000, 10)]}
        )
        stub_v2 = _StubSteam(
            result={"manifests": [_fake_manifest_payload(731, 200, "v2", 1_500, 15)]}
        )
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub_v1))
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub_v2))

        rows = await pool.read_all("SELECT version FROM manifests ORDER BY version")
        assert [r["version"] for r in rows] == ["100", "200"]


class TestErrorPaths:
    async def test_rejects_non_steam_platform(self, pool):
        # Epic games not yet supported by BL12.
        await pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned) "
            "VALUES ('epic', 'fortnite', 'Fortnite', 1)"
        )
        row = await pool.read_one("SELECT id FROM games WHERE app_id='fortnite'")
        epic_id = row["id"]
        job = _job(epic_id)
        job["platform"] = "epic"

        stub = _StubSteam()
        with pytest.raises(ValueError, match="manifest_fetch only supports steam"):
            await manifest_fetch_handler(job, Deps(pool=pool, steam_client=stub))

    async def test_requires_steam_client(self, pool):
        game_id = await _seed_game(pool)
        with pytest.raises(RuntimeError, match="steam_client is required"):
            await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=None))

    async def test_unknown_game_id_raises(self, pool):
        stub = _StubSteam()
        with pytest.raises(ValueError, match=r"game .* not found"):
            await manifest_fetch_handler(_job(99999), Deps(pool=pool, steam_client=stub))

    async def test_not_authenticated_flips_platforms_to_expired(self, pool):
        """Mirror library_sync's F-UAT6-3 behavior — NotAuthenticated
        flips platforms.auth_status='expired' before re-raising."""
        from orchestrator.platform.steam.client import SteamWorkerError

        # Pre-set platforms row to 'ok' so we can verify the flip.
        await pool.execute_write("UPDATE platforms SET auth_status='ok' WHERE name='steam'")
        game_id = await _seed_game(pool)
        stub = _StubSteam(raises=SteamWorkerError("NotAuthenticated", "no session"))

        with pytest.raises(SteamWorkerError):
            await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))

        row = await pool.read_one(
            "SELECT auth_status, last_error FROM platforms WHERE name='steam'"
        )
        assert row["auth_status"] == "expired"
        assert "NotAuthenticated" in row["last_error"]

    async def test_other_steam_error_does_not_flip_auth_status(self, pool):
        from orchestrator.platform.steam.client import SteamWorkerError

        await pool.execute_write("UPDATE platforms SET auth_status='ok' WHERE name='steam'")
        game_id = await _seed_game(pool)
        stub = _StubSteam(raises=SteamWorkerError("SteamAPIError", "transient"))

        with pytest.raises(SteamWorkerError):
            await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))

        row = await pool.read_one("SELECT auth_status FROM platforms WHERE name='steam'")
        assert row["auth_status"] == "ok"

    async def test_empty_manifests_result_no_rows_written(self, pool):
        """Game with no depots / no manifests — handler succeeds with
        zero inserts. games.size_bytes left unchanged."""
        game_id = await _seed_game(pool)
        # Pre-set a size to confirm it's not zeroed out.
        await pool.execute_write("UPDATE games SET size_bytes=? WHERE id=?", (12345, game_id))
        stub = _StubSteam(result={"manifests": []})
        await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))

        rows = await pool.read_all("SELECT id FROM manifests")
        assert rows == []
        row = await pool.read_one("SELECT size_bytes FROM games WHERE id=?", (game_id,))
        # No update happened — pre-existing size_bytes preserved
        assert row["size_bytes"] == 12345


class TestSizeCap:
    async def test_rejects_oversized_manifest(self, pool):
        """`manifest_size_cap_bytes` (Settings) is enforced per row. A
        manifest exceeding the cap raises before any DB write."""
        from unittest.mock import patch

        game_id = await _seed_game(pool)
        oversized = b"x" * 1024  # 1024-byte BLOB file
        stub = _StubSteam(
            result={
                "manifests": [
                    {
                        "depot_id": 731,
                        "manifest_gid": 1,
                        "name": "huge",
                        "total_bytes": 1_000_000_000,
                        "chunk_count": 100,
                        "raw_path": _write_blob(oversized),
                    }
                ]
            }
        )

        # Cap to 512 so the 1024-byte fake manifest exceeds it.
        with patch("orchestrator.jobs.handlers.manifest_fetch.get_settings") as mock_settings:
            from unittest.mock import MagicMock

            settings_stub = MagicMock()
            settings_stub.manifest_size_cap_bytes = 512
            mock_settings.return_value = settings_stub

            with pytest.raises(ValueError, match=r"manifest .* exceeds size cap"):
                await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))

        rows = await pool.read_all("SELECT id FROM manifests")
        assert rows == []

    async def test_size_cap_raise_cleans_up_unprocessed_temp_files(self, pool):
        """SEV-4 (review 2026-06-02): the worker writes EVERY depot's BLOB to a
        temp file up front. When an early entry trips the size cap and the
        handler raises, the not-yet-processed entries' temp files must not leak —
        the per-iteration `finally` only reaches the entry being processed."""
        from unittest.mock import MagicMock, patch

        game_id = await _seed_game(pool)
        oversized_path = _write_blob(b"x" * 1024)  # trips the cap → raises first
        unprocessed_path = _write_blob(b"y" * 10)  # never reached by the loop
        stub = _StubSteam(
            result={
                "manifests": [
                    {
                        "depot_id": 731,
                        "manifest_gid": 1,
                        "name": "huge",
                        "total_bytes": 1_000_000_000,
                        "chunk_count": 100,
                        "raw_path": oversized_path,
                    },
                    {
                        "depot_id": 734,
                        "manifest_gid": 2,
                        "name": "normal",
                        "total_bytes": 10,
                        "chunk_count": 1,
                        "raw_path": unprocessed_path,
                    },
                ]
            }
        )

        with patch("orchestrator.jobs.handlers.manifest_fetch.get_settings") as mock_settings:
            settings_stub = MagicMock()
            settings_stub.manifest_size_cap_bytes = 512
            mock_settings.return_value = settings_stub

            with pytest.raises(ValueError, match=r"exceeds size cap"):
                await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))

        assert not os.path.exists(oversized_path)
        assert not os.path.exists(unprocessed_path), "unprocessed depot temp file leaked"
