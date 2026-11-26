"""Feature B: re-check the whole library after an uncommanded cache eviction.

cache-catcher records each eviction alert it sends; the agent serves the file;
this poller (Karl's N1 — the control plane asks, nothing on the NAS calls in)
reads events newer than the one last acted on.

* A new event restarts the sweep pass at the event time (``restart_pass``), so
  every game measured before the eviction is measured again, and queues a sweep.
* While that forced pass is unfinished, a poll that finds no sweep queued or
  running queues the next one. The chaining lives here because a sweep cannot
  queue its own successor: its own row is still ``running``, so
  ``idx_jobs_sweep_inflight`` would drop the insert.

The acted-on timestamp is stored in ``sweep_pass.last_eviction_ts``, so a restart
of the control plane never acts on the same eviction twice. Forced sweeps carry
``source='trigger'`` and so never push the scheduler-only sweep monitor (180);
Kuma 220 already alerts on the eviction itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.jobs.sweep_pass import read_pass, restart_pass
from orchestrator.scheduler.jobs import enqueue_validation_sweep

if TYPE_CHECKING:
    from orchestrator.clients.agent_client import AgentClient
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)

PollResult = Literal["triggered", "chained", "idle", "error"]


class EvictionWatch:
    def __init__(self, pool: Pool, agent_client: AgentClient) -> None:
        self._pool = pool
        self._agent = agent_client

    async def poll(self) -> PollResult:
        """One poll. Never raises (APScheduler would only log it)."""
        try:
            return await self._poll()
        except Exception as e:
            _log.warning("eviction_watch.poll_failed", error=type(e).__name__, reason=str(e)[:200])
            return "error"

    async def _poll(self) -> PollResult:
        state = await read_pass(self._pool)
        try:
            events = await self._agent.eviction_events(state.last_eviction_ts)
        except Exception as e:
            # An older agent without the endpoint (404) or an unreachable one:
            # never trigger on missing information; the next poll retries.
            _log.warning("eviction_watch.agent_error", error=type(e).__name__, reason=str(e)[:200])
            return "error"

        ack = state.last_eviction_ts
        fresh = [e for e in events if ack is None or str(e["ts"]) > ack]
        if fresh:
            newest = max(fresh, key=lambda e: str(e["ts"]))
            ts = str(newest["ts"])
            await restart_pass(self._pool, started_at=ts, eviction_ts=ts)
            inserted = await enqueue_validation_sweep(self._pool, source="trigger")
            _log.warning(
                "eviction_watch.triggered",
                eviction_ts=ts,
                deletes=newest.get("deletes"),
                actor=newest.get("actor"),
                events=len(fresh),
                sweep_queued=bool(inserted),
            )
            return "triggered"

        if state.forced and not await self._sweep_in_flight():
            if await self._sweep_failed_recently():
                # A real mass eviction trips the measurement breaker, which fails
                # the sweep. Wait out its window rather than fail one every poll.
                _log.info("eviction_watch.chain_backoff", pass_number=state.number)
                return "idle"
            inserted = await enqueue_validation_sweep(self._pool, source="trigger")
            _log.info(
                "eviction_watch.chained", pass_number=state.number, sweep_queued=bool(inserted)
            )
            return "chained"
        return "idle"

    async def _sweep_in_flight(self) -> bool:
        row = await self._pool.read_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind='sweep' AND state IN ('queued','running')"
        )
        return bool(row and row["n"])

    async def _sweep_failed_recently(self) -> bool:
        """True when the most recent finished sweep failed within the measurement
        breaker's window (ledger ruling 6) — don't chain into a tripped breaker."""
        window_min = get_settings().measurement_breaker_window_minutes
        row = await self._pool.read_one(
            "SELECT state, finished_at > datetime('now', ?) AS recent FROM jobs "
            "WHERE kind='sweep' AND state IN ('succeeded','failed','cancelled') "
            "ORDER BY id DESC LIMIT 1",
            (f"-{int(window_min)} minutes",),
        )
        return bool(row and row["state"] == "failed" and row["recent"])
