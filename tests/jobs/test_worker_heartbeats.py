"""Which scheduled jobs get an Uptime Kuma heartbeat, and what it says.

UAT-14. Kuma marks a monitor DOWN when no heartbeat arrives inside its interval, so
these detect a job that silently stops running — the failure mode nothing else here
catches. The live arm found the existing poll monitor had taken 854 consecutive 403s
over nearly seven days without anyone noticing, which is the shape of the problem.

The non-obvious part is that **scheduled prefill is not its own job kind**. It is
``kind='prefill'`` with ``source='scheduler'``; a prefill you trigger by hand from the
CLI or from Game_shelf is the same kind. Heartbeating every prefill would push the
monitor up whenever you happened to click something, which is worse than no monitor —
it would report the schedule as healthy on the strength of manual activity.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.core.settings import Settings
from orchestrator.jobs.handlers import clear, register
from orchestrator.jobs.worker import Deps, monitor_url_for, worker_loop

SWEEP_URL = "http://kuma.example/api/push/sweep"
PREFILL_URL = "http://kuma.example/api/push/prefill"
LIBSYNC_URL = "http://kuma.example/api/push/libsync"


def _settings(**overrides) -> Settings:
    base = {
        "orch_token": "t" * 32,
        "kuma_push_sweep": SWEEP_URL,
        "kuma_push_scheduled_prefill": PREFILL_URL,
        "kuma_push_library_sync": LIBSYNC_URL,
    }
    base.update(overrides)
    return Settings(**base)


class TestMonitorSelection:
    """Pure mapping from a job to its monitor. No mocks — this is the real decision."""

    def test_sweep_maps_to_the_sweep_monitor(self) -> None:
        assert monitor_url_for("sweep", "scheduler", _settings()) == SWEEP_URL

    def test_library_sync_maps_to_its_monitor(self) -> None:
        assert monitor_url_for("library_sync", "scheduler", _settings()) == LIBSYNC_URL

    def test_a_scheduled_prefill_maps_to_the_prefill_monitor(self) -> None:
        assert monitor_url_for("prefill", "scheduler", _settings()) == PREFILL_URL

    @pytest.mark.parametrize("source", ["cli", "gameshelf", "api"])
    def test_a_hand_triggered_prefill_gets_no_heartbeat(self, source: str) -> None:
        assert monitor_url_for("prefill", source, _settings()) is None, (
            "a prefill you triggered yourself says nothing about whether the SCHEDULE "
            "is running; pushing up here would report a dead scheduler as healthy"
        )

    def test_a_kind_with_no_monitor_configured_gets_none(self) -> None:
        assert monitor_url_for("validate", "scheduler", _settings()) is None

    def test_an_unset_url_disables_that_heartbeat(self) -> None:
        assert monitor_url_for("sweep", "scheduler", _settings(kuma_push_sweep=None)) is None


@pytest.mark.asyncio
class TestWorkerEmitsHeartbeats:
    """The wiring: a finished job actually pushes."""

    async def _run_one_job(self, pool, monkeypatch, *, kind, source, handler):
        pushed: list[dict] = []

        async def fake_push(url, *, status, msg="", transport=None):
            pushed.append({"url": url, "status": status, "msg": msg})

        monkeypatch.setattr("orchestrator.jobs.worker.heartbeat.push", fake_push)
        monkeypatch.setattr("orchestrator.jobs.worker.get_settings", _settings)

        clear()
        register(kind, handler)
        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) VALUES (?, 'steam', 'queued', ?)",
            (kind, source),
        )

        shutdown = asyncio.Event()

        async def stopper():
            for _ in range(200):
                if pushed:
                    break
                await asyncio.sleep(0.01)
            shutdown.set()

        await asyncio.gather(
            worker_loop(Deps(pool=pool), shutdown=shutdown, poll_interval_sec=0.02),
            stopper(),
        )
        return pushed

    async def test_a_successful_sweep_pushes_up(self, pool, monkeypatch) -> None:
        async def ok(row, deps):
            return None

        pushed = await self._run_one_job(
            pool, monkeypatch, kind="sweep", source="scheduler", handler=ok
        )

        assert len(pushed) == 1
        assert pushed[0]["url"] == SWEEP_URL
        assert pushed[0]["status"] == "up"

    async def test_a_failed_sweep_pushes_down_carrying_the_error(self, pool, monkeypatch) -> None:
        async def boom(row, deps):
            raise RuntimeError("agent unreachable: ReadTimeout")

        pushed = await self._run_one_job(
            pool, monkeypatch, kind="sweep", source="scheduler", handler=boom
        )

        assert len(pushed) == 1
        assert pushed[0]["status"] == "down"
        assert "ReadTimeout" in pushed[0]["msg"], (
            "the monitor must carry why it failed, not just that it did"
        )

    async def test_a_hand_triggered_prefill_pushes_nothing(self, pool, monkeypatch) -> None:
        async def ok(row, deps):
            return None

        pushed = await self._run_one_job(
            pool, monkeypatch, kind="prefill", source="cli", handler=ok
        )

        assert pushed == []

    async def test_a_push_failure_does_not_fail_the_job(self, pool, monkeypatch) -> None:
        """Monitoring must never break the thing it monitors."""

        async def ok(row, deps):
            return None

        async def exploding_push(url, *, status, msg="", transport=None):
            raise RuntimeError("kuma is on fire")

        monkeypatch.setattr("orchestrator.jobs.worker.heartbeat.push", exploding_push)
        monkeypatch.setattr("orchestrator.jobs.worker.get_settings", _settings)

        clear()
        register("sweep", ok)
        await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source) "
            "VALUES ('sweep', 'steam', 'queued', 'scheduler')"
        )

        shutdown = asyncio.Event()

        async def stopper():
            for _ in range(200):
                row = await pool.read_one("SELECT state FROM jobs WHERE id=1")
                if row and row["state"] in ("succeeded", "failed"):
                    break
                await asyncio.sleep(0.01)
            shutdown.set()

        await asyncio.gather(
            worker_loop(Deps(pool=pool), shutdown=shutdown, poll_interval_sec=0.02),
            stopper(),
        )

        after = await pool.read_one("SELECT state FROM jobs WHERE id=1")
        assert after["state"] == "succeeded", (
            "the job succeeded; a heartbeat that could not be delivered must not change that"
        )
