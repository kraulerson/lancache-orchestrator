"""New - Not Cached: the new_games_check handler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.clients import heartbeat
from orchestrator.core.settings import Settings
from orchestrator.jobs import measurement
from orchestrator.jobs.handlers import new_games_check as mod
from orchestrator.jobs.handlers.new_games_check import new_games_check_handler
from orchestrator.jobs.new_games import overdue_message, select_overdue
from orchestrator.jobs.worker import Deps
from orchestrator.scheduler.jobs import enqueue_scheduled_prefill
from orchestrator.validator.disk_stat import ValidationResult

pytestmark = pytest.mark.asyncio

OVERDUE_URL = "http://kuma.example/api/push/overdue-secret"


def _ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _settings(**kw) -> Settings:
    base = {"orchestrator_token": "a" * 32, "kuma_push_new_games_overdue": OVERDUE_URL}
    base.update(kw)
    return Settings(**base)


async def _game(pool, app_id, *, status="unknown", seen=None, platform="steam"):
    # Test setup only: seeds status directly (writer guard scans src/ only).
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, first_seen_at) "
        "VALUES (?,?,?,1,?,?)",
        (platform, app_id, f"T{app_id}", status, seen if seen is not None else _ago(hours=1)),
    )
    row = await pool.read_one(
        "SELECT id FROM games WHERE platform=? AND app_id=?", (platform, app_id)
    )
    return int(row["id"])


class _Env:
    """Patches the handler's collaborators and records the calls."""

    def __init__(self, pool, monkeypatch, settings: Settings | None = None):
        self.pool = pool
        self.settings = settings or _settings()
        self.validated: list[int] = []
        self.pushes: list[tuple[str | None, str, str]] = []
        self.sync_calls: list[dict] = []
        self.outcome = "cached"
        self.outcomes: dict[int, str] = {}
        self.raise_for: dict[int, Exception] = {}
        self.sync_hook = None
        monkeypatch.setattr(mod, "get_settings", lambda: self.settings)
        monkeypatch.setattr(mod, "validate_one_game", self._validate)
        monkeypatch.setattr(mod, "_steam_library_sync", self._sync)
        monkeypatch.setattr(mod.heartbeat, "push", self._push)

    async def _validate(self, pool, deps, game_id, settings):
        self.validated.append(game_id)
        if game_id in self.raise_for:
            raise self.raise_for[game_id]
        return ValidationResult(1, 1, 0, self.outcomes.get(game_id, self.outcome), "1", None)

    async def _sync(self, job, deps):
        self.sync_calls.append(job)
        if self.sync_hook is not None:
            await self.sync_hook()

    async def _push(self, url, *, status, msg=""):
        self.pushes.append((url, status, msg))
        return True

    async def run(self, source="scheduler"):
        job = {"id": 9, "kind": "new_games_check", "source": source}
        return await new_games_check_handler(job, Deps(pool=self.pool))


async def test_runs_steam_sync_before_reading_check_set(pool, monkeypatch):
    env = _Env(pool, monkeypatch)

    async def _insert():
        await _game(pool, "77")

    env.sync_hook = _insert
    await env.run()
    row = await pool.read_one("SELECT id FROM games WHERE app_id='77'")
    assert env.validated == [row["id"]]


async def test_steam_sync_failure_is_logged_and_check_continues(pool, monkeypatch):
    from structlog.testing import capture_logs

    env = _Env(pool, monkeypatch)
    g = await _game(pool, "1")

    async def _boom():
        raise RuntimeError("agent down")

    env.sync_hook = _boom
    with capture_logs() as logs:
        summary = await env.run()
    assert env.validated == [g]
    assert summary.ok is True
    ev = [e for e in logs if e["event"] == "new_games.steam_sync_failed"]
    assert len(ev) == 1
    assert OVERDUE_URL not in repr(ev)


async def test_measures_each_game_in_check_set_once(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(days=2))
    b = await _game(pool, "2", status="not_downloaded", seen=_ago(days=1))
    await _game(pool, "3", status="up_to_date")
    await _game(pool, "4", seen=_ago(days=30))
    # trigger: only the check set (a scheduler run also re-measures overdue game 4).
    await env.run("trigger")
    assert env.validated == [a, b]


async def test_per_game_error_records_attempt_and_continues(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(days=2))
    b = await _game(pool, "2", seen=_ago(days=1))
    env.raise_for[a] = RuntimeError("agent timeout")
    summary = await env.run()
    assert env.validated == [a, b]
    row = await pool.read_one("SELECT status, last_measure_attempt_at FROM games WHERE id=?", (a,))
    assert row["last_measure_attempt_at"] is not None
    assert row["status"] == "unknown"
    assert summary.ok is True


async def test_breaker_trip_fails_the_job(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(days=2))
    await _game(pool, "2", seen=_ago(days=1))
    env.raise_for[a] = measurement.CircuitBreakerTripped("tripped")
    with pytest.raises(measurement.CircuitBreakerTripped):
        await env.run()
    assert env.validated == [a]


async def test_breaker_trip_still_pushes_overdue_before_reraising(pool, monkeypatch):
    """M5: a tripped breaker fails the job, but the overdue monitor still reports."""
    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(hours=1))
    await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    env.raise_for[a] = measurement.CircuitBreakerTripped("tripped")
    with pytest.raises(measurement.CircuitBreakerTripped):
        await env.run()
    overdue = await select_overdue(pool, window_days=7, unknown_hours=48)
    assert env.pushes == [(OVERDUE_URL, "down", overdue_message(overdue))]


async def test_breaker_trip_during_overdue_remeasure_pushes_and_reraises(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    o = await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    env.raise_for[o] = measurement.CircuitBreakerTripped("tripped")
    with pytest.raises(measurement.CircuitBreakerTripped):
        await env.run()
    assert [p[1] for p in env.pushes] == ["down"]


async def test_breaker_trip_with_api_source_pushes_nothing(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(hours=1))
    env.raise_for[a] = measurement.CircuitBreakerTripped("tripped")
    with pytest.raises(measurement.CircuitBreakerTripped):
        await env.run("api")
    assert env.pushes == []


async def test_breaker_trip_overdue_report_failure_does_not_mask_the_trip(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(hours=1))
    env.raise_for[a] = measurement.CircuitBreakerTripped("tripped")

    async def _select_fails(*a_, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(mod, "select_overdue", _select_fails)
    with pytest.raises(measurement.CircuitBreakerTripped):
        await env.run()


@pytest.mark.parametrize("source", ["scheduler", "trigger"])
async def test_overdue_push_down_with_message_on_scheduler_and_trigger(pool, monkeypatch, source):
    env = _Env(pool, monkeypatch)
    # Old enough to be overdue (not cached past the window), and not in the check set.
    await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    await env.run(source)
    overdue = await select_overdue(pool, window_days=7, unknown_hours=48)
    assert len(overdue) == 1
    assert env.pushes == [(OVERDUE_URL, "down", overdue_message(overdue))]


async def test_overdue_push_up_when_none_overdue(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    await _game(pool, "1")
    await env.run("scheduler")
    assert env.pushes == [(OVERDUE_URL, "up", overdue_message([]))]


@pytest.mark.parametrize("source", ["api", "cli", "gameshelf"])
async def test_no_overdue_push_for_other_sources(pool, monkeypatch, source):
    env = _Env(pool, monkeypatch)
    await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    await env.run(source)
    assert env.pushes == []


async def test_overdue_logs_one_line_per_game(pool, monkeypatch):
    from structlog.testing import capture_logs

    env = _Env(pool, monkeypatch)
    await _game(pool, "8", status="not_downloaded", seen=_ago(days=30))
    await _game(pool, "9", status="not_downloaded", seen=_ago(days=31))
    with capture_logs() as logs:
        await env.run()
    assert len([e for e in logs if e["event"] == "new_games.overdue"]) == 2


async def test_summary_message_counts(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    await _game(pool, "1", seen=_ago(hours=30))
    b = await _game(pool, "2", seen=_ago(hours=20))
    c = await _game(pool, "3", seen=_ago(hours=10))
    await _game(pool, "4", status="not_downloaded", seen=_ago(hours=300))
    env.outcomes[b] = "missing"
    env.raise_for[c] = RuntimeError("agent timeout")
    summary = await env.run()
    assert summary.ok is True
    assert summary.msg == "checked 3 new, 1 cached; overdue 1"


async def test_overdue_push_message_fits_kuma_and_keeps_the_count(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    for i in range(40):
        await _game(pool, f"o{i}", status="not_downloaded", seen=_ago(days=30 + i))
    await env.run()
    (_, status, msg) = env.pushes[0]
    assert status == "down"
    assert len(msg) <= heartbeat.MSG_MAX_CHARS
    assert msg.startswith("overdue ")


async def test_failed_attempt_stamp_is_logged_and_remaining_games_still_measured(pool, monkeypatch):
    from structlog.testing import capture_logs

    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(days=3))
    b = await _game(pool, "2", seen=_ago(days=2))
    env.raise_for[a] = RuntimeError("agent timeout")

    async def _stamp_fails(pool_, game_id, outcome, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(mod, "record_measurement", _stamp_fails)
    with capture_logs() as logs:
        summary = await env.run()
    assert env.validated == [a, b]
    assert summary.msg.startswith("checked 2 new, 1 cached")
    assert [e for e in logs if e["event"] == "new_games.attempt_record_failed"]


async def test_new_epic_game_becomes_prefill_eligible(pool, monkeypatch):
    import orchestrator.jobs.handlers.validate as validate_mod

    env = _Env(pool, monkeypatch)
    # Use the real validate_one_game; only the disk measurement is faked.
    monkeypatch.setattr(mod, "validate_one_game", validate_mod.validate_one_game)

    async def _missing(pool_, deps_, game_id, settings_):
        return ValidationResult(10, 0, 10, "missing", "1", None)

    monkeypatch.setattr(validate_mod, "validate_game", _missing)
    g = await _game(pool, "ep1", platform="epic")
    assert await enqueue_scheduled_prefill(pool) == 0  # unmeasured: no traffic
    await env.run()
    row = await pool.read_one("SELECT status, status_measured_at FROM games WHERE id=?", (g,))
    assert row["status"] == "not_downloaded"
    assert row["status_measured_at"] is not None
    assert await enqueue_scheduled_prefill(pool) == 1


# --- I2: scheduler checks re-measure overdue "not cached" games (ruling 16) ---


async def test_scheduler_run_remeasures_overdue_not_cached_games(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(hours=1))
    o = await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    await _game(pool, "10", status="up_to_date", seen=_ago(days=30))  # cached: not overdue
    await env.run("scheduler")
    assert env.validated == [a, o]


@pytest.mark.parametrize("source", ["trigger", "api", "cli"])
async def test_non_scheduler_runs_do_not_remeasure_overdue(pool, monkeypatch, source):
    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(hours=1))
    await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    await env.run(source)
    assert env.validated == [a]


async def test_overdue_remeasure_skips_unmeasured_and_check_set_games(pool, monkeypatch):
    """Only reason 'not cached' is re-measured, and never a game the check set just
    measured (de-duplicated)."""
    from orchestrator.jobs.new_games import OverdueGame

    env = _Env(pool, monkeypatch)
    a = await _game(pool, "1", seen=_ago(hours=1))
    o = await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    real = mod.select_overdue

    async def _with_extras(pool_, **kw):
        rows = await real(pool_, **kw)
        return [
            OverdueGame(a, "T1", "not cached"),  # also in the check set
            OverdueGame(4242, "ghost", "unmeasured"),
            *rows,
        ]

    monkeypatch.setattr(mod, "select_overdue", _with_extras)
    await env.run("scheduler")
    assert env.validated == [a, o]


async def test_overdue_remeasure_error_is_isolated(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    o1 = await _game(pool, "8", status="not_downloaded", seen=_ago(days=31))
    o2 = await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    env.raise_for[o1] = RuntimeError("agent timeout")
    summary = await env.run("scheduler")
    assert env.validated == [o1, o2]
    assert summary.ok is True
    row = await pool.read_one("SELECT last_measure_attempt_at FROM games WHERE id=?", (o1,))
    assert row["last_measure_attempt_at"] is not None


async def test_overdue_cleared_by_remeasure_pushes_up(pool, monkeypatch):
    """The point of ruling 16: a fixed game clears the alert at the next scheduled run."""
    import orchestrator.jobs.handlers.validate as validate_mod

    env = _Env(pool, monkeypatch)
    monkeypatch.setattr(mod, "validate_one_game", validate_mod.validate_one_game)

    async def _cached(pool_, deps_, game_id, settings_):
        return ValidationResult(10, 10, 0, "cached", "1", None)

    monkeypatch.setattr(validate_mod, "validate_game", _cached)
    await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    await env.run("scheduler")
    assert env.pushes == [(OVERDUE_URL, "up", overdue_message([]))]


# --- I1: the handler gates on the job's CURRENT source, not the claim-time one ---


async def test_promoted_source_remeasures_overdue_and_pushes(pool, monkeypatch):
    env = _Env(pool, monkeypatch)
    o = await _game(pool, "9", status="not_downloaded", seen=_ago(days=30))
    await pool.execute_write(
        "INSERT INTO jobs (kind, state, source) VALUES ('new_games_check', 'running', 'scheduler')"
    )
    row = await pool.read_one("SELECT id FROM jobs WHERE kind='new_games_check'")
    # Claimed as an API run; the cron promoted the row to 'scheduler' after the claim.
    job = {"id": int(row["id"]), "kind": "new_games_check", "source": "api"}
    await new_games_check_handler(job, Deps(pool=pool))
    assert env.validated == [o]
    assert [p[0] for p in env.pushes] == [OVERDUE_URL]
