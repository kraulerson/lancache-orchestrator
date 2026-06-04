"""Tests for orchestrator.jobs.handlers.prefill (F5)."""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.prefill import prefill_handler
from orchestrator.jobs.worker import Deps
from orchestrator.prefill.downloader import PrefillResult

pytestmark = pytest.mark.asyncio

SHA_A = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"
SHA_B = "234a47ed3005727db220987ecac460030295bd79"


class _StubSteam:
    def __init__(self, expand=None):
        self._expand = expand or {"depot_id": 731, "chunk_shas": [SHA_A, SHA_B]}
        self.fetch_calls = 0

    async def manifest_expand(self, raw):
        return self._expand

    async def manifest_fetch(self, app_id):
        self.fetch_calls += 1
        return {"manifests": []}


def _job(game_id, platform="steam"):
    return {"id": 1, "kind": "prefill", "platform": platform, "game_id": game_id}


async def _seed_game(pool, *, platform="steam", app_id="730"):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned) VALUES (?, ?, 't', 1)",
        (platform, app_id),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


async def _seed_manifest(pool, game_id, *, depot_id=731, version="100"):
    await pool.execute_write(
        "INSERT INTO manifests "
        "(game_id, depot_id, version, fetched_at, chunk_count, total_bytes, raw) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, 100, ?)",
        (game_id, depot_id, version, b"BLOB"),
    )


async def test_downloading_set_and_validate_enqueued_on_success(pool, monkeypatch):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)

    async def fake_prefill(uris, settings, *, on_progress=None):
        return PrefillResult(len(uris), len(uris), 0)

    monkeypatch.setattr("orchestrator.jobs.handlers.prefill.prefill_chunks", fake_prefill)
    await prefill_handler(_job(game_id), Deps(pool=pool, steam_client=_StubSteam()))

    vj = await pool.read_one(
        "SELECT kind, state FROM jobs WHERE kind='validate' AND game_id=?", (game_id,)
    )
    assert vj == {"kind": "validate", "state": "queued"}
    g = await pool.read_one("SELECT last_prefilled_at FROM games WHERE id=?", (game_id,))
    assert g["last_prefilled_at"] is not None


async def test_chunk_failure_marks_game_failed(pool, monkeypatch):
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)

    async def fake_prefill(uris, settings, *, on_progress=None):
        return PrefillResult(len(uris), 0, len(uris), [("/depot/731/chunk/x", "http 500")])

    monkeypatch.setattr("orchestrator.jobs.handlers.prefill.prefill_chunks", fake_prefill)
    with pytest.raises(RuntimeError):
        await prefill_handler(_job(game_id), Deps(pool=pool, steam_client=_StubSteam()))
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "failed"
    vj = await pool.read_one("SELECT id FROM jobs WHERE kind='validate' AND game_id=?", (game_id,))
    assert vj is None


async def test_manifest_error_marks_failed_not_stuck_downloading(pool):
    """An IPC/worker failure during manifest expand must not leave the game
    stuck in 'downloading' forever — it must resolve to 'failed' (UAT-10 #2)."""
    game_id = await _seed_game(pool)
    await _seed_manifest(pool, game_id)

    class _BoomSteam(_StubSteam):
        async def manifest_expand(self, raw):
            raise RuntimeError("worker died: IPC timeout")

    with pytest.raises(RuntimeError):
        await prefill_handler(_job(game_id), Deps(pool=pool, steam_client=_BoomSteam()))
    g = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    assert g["status"] == "failed"


async def test_no_manifests_triggers_fetch(pool, monkeypatch):
    game_id = await _seed_game(pool)  # no manifest rows

    async def fake_prefill(uris, settings, *, on_progress=None):
        return PrefillResult(len(uris), len(uris), 0)

    monkeypatch.setattr("orchestrator.jobs.handlers.prefill.prefill_chunks", fake_prefill)

    stub = _StubSteam()

    async def fake_fetch(app_id):
        stub.fetch_calls += 1
        await _seed_manifest(pool, game_id)  # fetch populates manifests
        return {"manifests": [{}]}

    stub.manifest_fetch = fake_fetch  # type: ignore[method-assign]
    await prefill_handler(_job(game_id), Deps(pool=pool, steam_client=stub))
    assert stub.fetch_calls == 1


async def test_unsupported_platform_raises(pool):
    # steam + epic are supported (F6); an unknown platform still rejects.
    game_id = await _seed_game(pool, platform="epic", app_id="fort")
    with pytest.raises(ValueError, match="unsupported platform"):
        await prefill_handler(_job(game_id, "gog"), Deps(pool=pool, steam_client=_StubSteam()))


async def test_unknown_game_raises(pool):
    with pytest.raises(ValueError, match="not found"):
        await prefill_handler(_job(99999), Deps(pool=pool, steam_client=_StubSteam()))


async def test_registered():
    from orchestrator.jobs.handlers import HANDLERS, _register_builtin_handlers

    _register_builtin_handlers()
    assert "prefill" in HANDLERS
