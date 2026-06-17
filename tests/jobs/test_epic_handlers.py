"""F6: Epic branches of the library_sync + prefill job handlers."""

from __future__ import annotations

import json

import pytest

import orchestrator.jobs.handlers.prefill as ph
from orchestrator.jobs.handlers.library_sync import library_sync_handler
from orchestrator.jobs.handlers.prefill import prefill_handler
from orchestrator.jobs.worker import Deps
from orchestrator.platform.epic.models import (
    EpicChunk,
    EpicLibraryItem,
    EpicManifest,
)
from orchestrator.prefill.epic_downloader import EpicPrefillResult

pytestmark = pytest.mark.asyncio


class _StubEpic:
    def __init__(self, items=None, manifest=None):
        self._items = items or []
        self._manifest = manifest

    async def library_enumerate(self):
        return self._items

    async def fetch_manifest(self, item):
        return self._manifest, "epiccdn.test", "/base"


def _job(kind, game_id=None):
    return {"id": 1, "kind": kind, "platform": "epic", "game_id": game_id}


async def _seed_epic_game(pool, app_id="AppA", title="Game A"):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, metadata) "
        "VALUES ('epic', ?, ?, 1, 'not_downloaded', ?)",
        (app_id, title, json.dumps({"namespace": "ns", "catalog_item_id": "cat"})),
    )
    row = await pool.read_one("SELECT id FROM games WHERE platform='epic' AND app_id=?", (app_id,))
    return row["id"]


async def test_epic_library_sync_upserts_games(pool):
    stub = _StubEpic(items=[EpicLibraryItem("AppA", "ns", "cat", "Game A")])
    await library_sync_handler(
        _job("library_sync"), Deps(pool=pool, steam_client=None, epic_client=stub)
    )
    row = await pool.read_one(
        "SELECT platform, app_id, title, metadata FROM games WHERE platform='epic'"
    )
    assert row["app_id"] == "AppA"
    assert row["title"] == "Game A"
    meta = json.loads(row["metadata"])
    assert meta["namespace"] == "ns"
    assert meta["catalog_item_id"] == "cat"


async def test_epic_library_sync_requires_client(pool):
    with pytest.raises(RuntimeError):
        await library_sync_handler(
            _job("library_sync"), Deps(pool=pool, steam_client=None, epic_client=None)
        )


def _manifest():
    chunks = [EpicChunk((1, 2, 3, 4), 100, b"x" * 20, 0, 500, 1048576)]
    return EpicManifest(version=22, chunks=chunks, cdn_base="/base", raw=b"BINARY-MANIFEST")


async def test_epic_prefill_downloads_stores_manifest_marks_up_to_date(pool, monkeypatch):
    gid = await _seed_epic_game(pool)
    stub = _StubEpic(manifest=_manifest())

    async def fake_prefill(paths, host, base, settings, **kw):
        return EpicPrefillResult(len(paths), len(paths), 0)

    async def fake_verify(paths, host, base, settings, **kw):
        return 1.0

    monkeypatch.setattr(ph, "epic_prefill_chunks", fake_prefill)
    monkeypatch.setattr(ph, "epic_verify_cached", fake_verify)

    await prefill_handler(
        _job("prefill", gid), Deps(pool=pool, steam_client=None, epic_client=stub)
    )

    g = await pool.read_one("SELECT status, size_bytes FROM games WHERE id=?", (gid,))
    assert g["status"] == "up_to_date"
    assert g["size_bytes"] == 500
    m = await pool.read_one(
        "SELECT version, chunk_count, total_bytes FROM manifests WHERE game_id=?", (gid,)
    )
    assert m["chunk_count"] == 1
    assert m["total_bytes"] == 500


async def test_epic_prefill_low_hit_ratio_is_non_gating(pool, monkeypatch):
    """A low post-prefill HIT ratio logs a warning but does NOT fail the job —
    lancache caches asynchronously, so an immediate re-request can legitimately
    MISS; download success is the success signal (UAT-10 #8)."""
    gid = await _seed_epic_game(pool, app_id="AppE")
    stub = _StubEpic(manifest=_manifest())

    async def fake_prefill(paths, host, base, settings, **kw):
        return EpicPrefillResult(len(paths), len(paths), 0)

    async def fake_verify(paths, host, base, settings, **kw):
        return 0.2  # below the 0.5 warning threshold

    monkeypatch.setattr(ph, "epic_prefill_chunks", fake_prefill)
    monkeypatch.setattr(ph, "epic_verify_cached", fake_verify)

    await prefill_handler(
        _job("prefill", gid), Deps(pool=pool, steam_client=None, epic_client=stub)
    )
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (gid,))
    assert g["status"] == "up_to_date"  # informational, not a failure


async def test_epic_prefill_failed_chunks_marks_failed(pool, monkeypatch):
    gid = await _seed_epic_game(pool, app_id="AppB")
    stub = _StubEpic(manifest=_manifest())

    async def fake_prefill(paths, host, base, settings, **kw):
        return EpicPrefillResult(len(paths), 0, len(paths))

    monkeypatch.setattr(ph, "epic_prefill_chunks", fake_prefill)

    with pytest.raises(RuntimeError):
        await prefill_handler(
            _job("prefill", gid), Deps(pool=pool, steam_client=None, epic_client=stub)
        )
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (gid,))
    assert g["status"] == "failed"


async def test_epic_prefill_manifest_error_marks_failed_not_stuck_downloading(pool):
    gid = await _seed_epic_game(pool, app_id="AppD")

    class _BadEpic:
        async def fetch_manifest(self, item):
            from orchestrator.platform.epic.manifest import EpicManifestError

            raise EpicManifestError("boom")

    from orchestrator.platform.epic.manifest import EpicManifestError

    with pytest.raises(EpicManifestError):
        await prefill_handler(
            _job("prefill", gid), Deps(pool=pool, steam_client=None, epic_client=_BadEpic())
        )
    g = await pool.read_one("SELECT status, last_error FROM games WHERE id=?", (gid,))
    assert g["status"] == "failed"  # not stuck in 'downloading'
    assert "EpicManifestError" in (g["last_error"] or "")


async def test_epic_prefill_requires_client(pool):
    gid = await _seed_epic_game(pool, app_id="AppC")
    with pytest.raises(RuntimeError):
        await prefill_handler(
            _job("prefill", gid), Deps(pool=pool, steam_client=None, epic_client=None)
        )


async def test_epic_library_sync_writes_current_version(pool):
    stub = _StubEpic(items=[EpicLibraryItem("AppA", "ns", "cat", "Game A", build_version="bv-1")])
    await library_sync_handler(
        _job("library_sync"), Deps(pool=pool, steam_client=None, epic_client=stub)
    )
    row = await pool.read_one("SELECT current_version FROM games WHERE platform='epic'")
    assert row["current_version"] == "bv-1"


async def test_epic_prefill_sets_cached_version(pool, monkeypatch):
    gid = await _seed_epic_game(pool)
    await pool.execute_write("UPDATE games SET current_version='bv-1' WHERE id=?", (gid,))
    stub = _StubEpic(manifest=_manifest())

    async def fake_prefill(paths, host, base, settings, **kw):
        return EpicPrefillResult(len(paths), len(paths), 0)

    async def fake_verify(paths, host, base, settings, **kw):
        return 1.0

    monkeypatch.setattr(ph, "epic_prefill_chunks", fake_prefill)
    monkeypatch.setattr(ph, "epic_verify_cached", fake_verify)
    await prefill_handler(
        _job("prefill", gid), Deps(pool=pool, steam_client=None, epic_client=stub)
    )
    g = await pool.read_one("SELECT cached_version FROM games WHERE id=?", (gid,))
    assert g["cached_version"] == "bv-1"
