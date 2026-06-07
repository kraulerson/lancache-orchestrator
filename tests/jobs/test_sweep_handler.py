"""F13: scheduled validation sweep handler."""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.sweep import sweep_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


class _StubSteam:
    def __init__(self, response):
        self._response = response

    async def manifest_expand(self, raw: bytes):
        return self._response


def _job():
    return {"id": 1, "kind": "sweep", "platform": None, "game_id": None}


async def _seed(pool, *, platform="steam", status="up_to_date", app_id="730"):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status) VALUES (?, ?, 't', 1, ?)",
        (platform, app_id, status),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return row["id"]


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(tmp_path))
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def test_sweep_skips_when_validator_unhealthy(pool, tmp_path, monkeypatch):
    # Point the cache path at a non-existent dir → validator_self_test False.
    monkeypatch.setenv("ORCH_LANCACHE_NGINX_CACHE_PATH", str(tmp_path / "nope"))
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    await _seed(pool)
    # Must NOT raise (skip + succeed); the game is untouched.
    await sweep_handler(_job(), Deps(pool=pool, steam_client=_StubSteam({})))
    g = await pool.read_one("SELECT status FROM games WHERE app_id='730'")
    assert g["status"] == "up_to_date"
    get_settings.cache_clear()


async def test_sweep_skips_when_no_steam_client(pool, cache_root):
    await _seed(pool)
    # steam_client None → skip + succeed, no raise.
    await sweep_handler(_job(), Deps(pool=pool, steam_client=None))
    g = await pool.read_one("SELECT status FROM games WHERE app_id='730'")
    assert g["status"] == "up_to_date"


async def test_sweep_validates_only_candidate_steam_games(pool, cache_root, monkeypatch):
    # candidates: up_to_date + validation_failed steam. NOT: epic, blocked, not_downloaded.
    g_ok = await _seed(pool, status="up_to_date", app_id="1")
    g_vf = await _seed(pool, status="validation_failed", app_id="2")
    await _seed(pool, status="blocked", app_id="3")
    await _seed(pool, status="not_downloaded", app_id="4")
    await _seed(pool, platform="epic", status="up_to_date", app_id="5")

    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        from orchestrator.validator.disk_stat import ValidationResult

        return ValidationResult(
            chunks_total=1,
            chunks_cached=1,
            chunks_missing=0,
            outcome="cached",
            manifest_version="100",
            error=None,
        )

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    await sweep_handler(_job(), Deps(pool=pool, steam_client=_StubSteam({})))
    assert sorted(seen) == sorted([g_ok, g_vf])


async def test_sweep_isolates_per_game_errors(pool, cache_root, monkeypatch):
    g1 = await _seed(pool, app_id="1")
    g2 = await _seed(pool, app_id="2")

    validated: list[int] = []

    async def flaky_validate_one(pool_, deps_, game_id, settings):
        from orchestrator.validator.disk_stat import ValidationResult

        if game_id == g1:
            raise RuntimeError("boom")
        validated.append(game_id)
        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", flaky_validate_one)
    # One game raising must NOT abort the sweep.
    await sweep_handler(_job(), Deps(pool=pool, steam_client=_StubSteam({})))
    assert validated == [g2]


async def test_sweep_error_outcome_not_counted_as_evicted(pool, cache_root, monkeypatch):
    """An 'error' validation outcome (cache hiccup, purged manifests) is NOT an
    eviction — the game keeps its up_to_date status, so it must not inflate the
    `evicted` drift metric, and must be surfaced as `validation_error` (F13
    adversarial-review finding 1)."""
    import structlog.testing as st

    import orchestrator.jobs.handlers.sweep as sweep_mod

    await _seed(pool, status="up_to_date", app_id="1")

    async def err_validate_one(pool_, deps_, game_id, settings):
        from orchestrator.validator.disk_stat import ValidationResult

        return ValidationResult(0, 0, 0, "error", "", "transient infra error")

    cap = st.CapturingLogger()
    monkeypatch.setattr(sweep_mod, "validate_one_game", err_validate_one)
    monkeypatch.setattr(sweep_mod, "_log", cap)
    await sweep_handler(_job(), Deps(pool=pool, steam_client=_StubSteam({})))

    done = [c for c in cap.calls if c.args and c.args[0] == "sweep.completed"]
    assert done, "sweep.completed not logged"
    assert done[0].kwargs["evicted"] == 0  # error != eviction
    assert done[0].kwargs["validation_error"] == 1  # surfaced, not silently absent


async def test_sweep_registered():
    from orchestrator.jobs.handlers import HANDLERS, _register_builtin_handlers

    _register_builtin_handlers()
    assert "sweep" in HANDLERS
