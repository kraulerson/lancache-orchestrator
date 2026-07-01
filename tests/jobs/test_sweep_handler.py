"""F13: scheduled validation sweep handler."""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.sweep import sweep_handler
from orchestrator.jobs.worker import Deps

pytestmark = pytest.mark.asyncio


class _Agent:
    """Truthy AgentClient stand-in. The sweep tests monkeypatch
    ``validate_one_game``, so steam_validate is never actually called; the agent
    only needs to be non-None to pass the handler's pre-flight guard."""


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


def _healthy(monkeypatch):
    """Force the validator self-test to pass so the sweep proceeds."""

    async def _ok(settings, *, agent_client=None):
        return True

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validator_self_test", _ok)


async def test_sweep_skips_when_validator_unhealthy(pool, monkeypatch):
    async def _unhealthy(settings, *, agent_client=None):
        return False

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validator_self_test", _unhealthy)
    await _seed(pool)
    # Must NOT raise (skip + succeed); the game is untouched.
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))
    g = await pool.read_one("SELECT status FROM games WHERE app_id='730'")
    assert g["status"] == "up_to_date"


async def test_sweep_skips_when_no_agent_client(pool):
    await _seed(pool)
    # agent_client None → skip + succeed, no raise.
    await sweep_handler(_job(), Deps(pool=pool, agent_client=None))
    g = await pool.read_one("SELECT status FROM games WHERE app_id='730'")
    assert g["status"] == "up_to_date"


async def test_sweep_validates_candidate_games_all_platforms(pool, monkeypatch):
    # candidates: up_to_date + validation_failed, any platform.
    # NOT: blocked, not_downloaded (regardless of platform).
    _healthy(monkeypatch)
    g_ok = await _seed(pool, status="up_to_date", app_id="1")
    g_vf = await _seed(pool, status="validation_failed", app_id="2")
    await _seed(pool, status="blocked", app_id="3")
    await _seed(pool, status="not_downloaded", app_id="4")
    g_epic = await _seed(pool, platform="epic", status="up_to_date", app_id="5")

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
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))
    # Steam + epic both swept; blocked / not_downloaded excluded regardless of platform.
    assert sorted(seen) == sorted([g_ok, g_vf, g_epic])


async def test_sweep_isolates_per_game_errors(pool, monkeypatch):
    _healthy(monkeypatch)
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
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))
    assert validated == [g2]


async def test_sweep_error_outcome_not_counted_as_evicted(pool, monkeypatch):
    """An 'error' validation outcome (cache hiccup, purged manifests) is NOT an
    eviction — the game keeps its up_to_date status, so it must not inflate the
    `evicted` drift metric, and must be surfaced as `validation_error` (F13
    adversarial-review finding 1)."""
    import structlog.testing as st

    import orchestrator.jobs.handlers.sweep as sweep_mod

    _healthy(monkeypatch)
    await _seed(pool, status="up_to_date", app_id="1")

    async def err_validate_one(pool_, deps_, game_id, settings):
        from orchestrator.validator.disk_stat import ValidationResult

        return ValidationResult(0, 0, 0, "error", "", "transient infra error")

    cap = st.CapturingLogger()
    monkeypatch.setattr(sweep_mod, "validate_one_game", err_validate_one)
    monkeypatch.setattr(sweep_mod, "_log", cap)
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))

    done = [c for c in cap.calls if c.args and c.args[0] == "sweep.completed"]
    assert done, "sweep.completed not logged"
    assert done[0].kwargs["evicted"] == 0  # error != eviction
    assert done[0].kwargs["validation_error"] == 1  # surfaced, not silently absent


async def test_sweep_skips_when_agent_reports_validator_unhealthy(pool, monkeypatch, tmp_path):
    """re-arch ④: when agent_enabled, the sweep's pre-flight self-test sources
    health from the agent (not the local cache path). Even with a perfectly
    valid LOCAL cache path, an agent reporting validator_healthy=False must skip
    the sweep — proving the agent_client is actually threaded into the pre-flight
    gate. If the agent_client were NOT passed, the valid local path would report
    healthy and the sweep would proceed to call validate_one_game."""
    import structlog.testing as st

    import orchestrator.jobs.handlers.sweep as sweep_mod
    from orchestrator.core.settings import Settings

    (tmp_path / "ab").mkdir()  # a perfectly healthy LOCAL cache dir

    def _agent_settings():
        return Settings(
            orchestrator_token="a" * 32,
            agent_enabled=True,
            lancache_nginx_cache_path=tmp_path,
        )

    monkeypatch.setattr(sweep_mod, "get_settings", _agent_settings)

    # If the agent gate were bypassed, the (valid) local path would let the sweep
    # proceed and this would run, flipping the sentinel.
    ran = {"validated": False}

    async def _track(*a, **k):
        ran["validated"] = True
        from orchestrator.validator.disk_stat import ValidationResult

        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr(sweep_mod, "validate_one_game", _track)

    class _UnhealthyAgent:
        async def agent_health(self):
            return {"ok": True, "validator_healthy": False}

    cap = st.CapturingLogger()
    monkeypatch.setattr(sweep_mod, "_log", cap)

    await _seed(pool)
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_UnhealthyAgent()))

    assert ran["validated"] is False  # sweep never reached validation
    skips = [c for c in cap.calls if c.args and c.args[0] == "sweep.skipped"]
    assert skips, "sweep.skipped not logged"
    assert skips[0].kwargs["reason"] == "validator_unhealthy"
    g = await pool.read_one("SELECT status FROM games WHERE app_id='730'")
    assert g["status"] == "up_to_date"  # game untouched


async def test_sweep_registered():
    from orchestrator.jobs.handlers import HANDLERS, _register_builtin_handlers

    _register_builtin_handlers()
    assert "sweep" in HANDLERS


def _capture_pool(pool, captured):
    """Wrap pool.read_all to record the candidate SQL the sweep selects with."""
    real = pool.read_all

    async def _spy(sql, *args):
        captured["sql"] = sql
        return await real(sql, *args)

    pool.read_all = _spy
    return pool


async def test_full_payload_selects_all_platforms(pool, monkeypatch):
    """`{"full": true}` payload => the validate-all candidate SQL (every game,
    all platforms, no status gate)."""
    import orchestrator.jobs.handlers.sweep as sweep_mod

    _healthy(monkeypatch)
    captured: dict[str, str] = {}
    _capture_pool(pool, captured)
    job = {"id": 1, "kind": "sweep", "payload": '{"full": true}'}
    await sweep_handler(job, Deps(pool=pool, agent_client=_Agent()))
    assert captured["sql"] == sweep_mod._CANDIDATE_SQL_FULL
    assert "status IN" not in captured["sql"]


async def test_default_payload_keeps_status_gated(pool, monkeypatch):
    """A null payload keeps the status-gated weekly-cron candidate SQL."""
    import orchestrator.jobs.handlers.sweep as sweep_mod

    _healthy(monkeypatch)
    captured: dict[str, str] = {}
    _capture_pool(pool, captured)
    job = {"id": 1, "kind": "sweep", "payload": None}
    await sweep_handler(job, Deps(pool=pool, agent_client=_Agent()))
    assert captured["sql"] == sweep_mod._CANDIDATE_SQL
    assert "status IN ('up_to_date','validation_failed')" in captured["sql"]


async def test_malformed_payload_falls_back_to_gated(pool, monkeypatch):
    """A non-JSON payload must not raise — fall back to the status-gated sweep."""
    import orchestrator.jobs.handlers.sweep as sweep_mod

    _healthy(monkeypatch)
    captured: dict[str, str] = {}
    _capture_pool(pool, captured)
    job = {"id": 1, "kind": "sweep", "payload": "not json"}
    await sweep_handler(job, Deps(pool=pool, agent_client=_Agent()))
    assert captured["sql"] == sweep_mod._CANDIDATE_SQL


# ---------------------------------------------------------------------------
# Task 8: un-scope sweep to include Epic games
# ---------------------------------------------------------------------------


async def test_sweep_includes_epic_games_status_gated(pool, monkeypatch):
    """Status-gated sweep must validate Epic games (up_to_date / validation_failed)
    alongside Steam games — not just Steam (Task 8, epic-validation-parity)."""
    _healthy(monkeypatch)
    steam_id = await _seed(pool, platform="steam", status="up_to_date", app_id="1")
    epic_id = await _seed(pool, platform="epic", status="up_to_date", app_id="fortnite")
    epic_vf_id = await _seed(pool, platform="epic", status="validation_failed", app_id="turaco")

    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        from orchestrator.validator.disk_stat import ValidationResult

        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    await sweep_handler(_job(), Deps(pool=pool, agent_client=_Agent()))
    assert steam_id in seen, "steam game must be validated"
    assert epic_id in seen, "epic up_to_date game must be validated"
    assert epic_vf_id in seen, "epic validation_failed game must be validated"


async def test_sweep_includes_epic_games_full(pool, monkeypatch):
    """Full-mode sweep must validate Epic games alongside Steam games
    (Task 8, epic-validation-parity)."""
    _healthy(monkeypatch)
    steam_id = await _seed(pool, platform="steam", status="not_downloaded", app_id="10")
    epic_id = await _seed(pool, platform="epic", status="not_downloaded", app_id="nd_epic")

    seen: list[int] = []

    async def fake_validate_one(pool_, deps_, game_id, settings):
        seen.append(game_id)
        from orchestrator.validator.disk_stat import ValidationResult

        return ValidationResult(1, 1, 0, "cached", "100", None)

    monkeypatch.setattr("orchestrator.jobs.handlers.sweep.validate_one_game", fake_validate_one)
    job = {"id": 1, "kind": "sweep", "payload": '{"full": true}'}
    await sweep_handler(job, Deps(pool=pool, agent_client=_Agent()))
    assert steam_id in seen, "steam game must be validated in full mode"
    assert epic_id in seen, "epic game must be validated in full mode"
