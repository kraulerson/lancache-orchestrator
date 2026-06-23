"""Tests for the agent's ephemeral in-memory job registry."""

from __future__ import annotations

import pytest

from orchestrator.agent.jobs import AgentJobStore

pytestmark = pytest.mark.asyncio


async def test_create_starts_running():
    store = AgentJobStore()
    job_id = store.create()
    snap = store.get(job_id)
    assert snap["state"] == "running"
    assert snap["done"] == 0
    assert snap["total"] == 0


async def test_progress_updates():
    store = AgentJobStore()
    job_id = store.create()
    store.set_progress(job_id, 7, 20)
    snap = store.get(job_id)
    assert (snap["done"], snap["total"]) == (7, 20)
    assert snap["state"] == "running"


async def test_done_carries_result():
    store = AgentJobStore()
    job_id = store.create()
    store.set_done(job_id, {"chunks_ok": 5})
    snap = store.get(job_id)
    assert snap["state"] == "done"
    assert snap["result"] == {"chunks_ok": 5}


async def test_failed_carries_error():
    store = AgentJobStore()
    job_id = store.create()
    store.set_failed(job_id, "boom")
    snap = store.get(job_id)
    assert snap["state"] == "failed"
    assert snap["error"] == "boom"


async def test_unknown_job_is_none():
    assert AgentJobStore().get("nope") is None


async def test_evicts_oldest_terminal_jobs_over_cap():
    """MEM-1 (review 2026-06-23): the long-lived agent must not grow unbounded —
    once over the cap, the OLDEST terminal (done/failed) jobs are trimmed."""
    store = AgentJobStore(max_jobs=3)
    ids = [store.create() for _ in range(3)]
    for jid in ids:
        store.set_done(jid, {"ok": True})
    # A 4th create pushes over the cap → the oldest terminal job is evicted.
    fourth = store.create()
    assert store.get(ids[0]) is None  # oldest evicted
    assert store.get(ids[1]) is not None
    assert store.get(ids[2]) is not None
    assert store.get(fourth) is not None
    assert store.size() == 3


async def test_never_evicts_running_jobs():
    """In-flight jobs are never evicted even past the cap — the control plane is
    still polling them. Only terminal jobs are trimmed."""
    store = AgentJobStore(max_jobs=2)
    running = [store.create() for _ in range(3)]  # all running, none terminal
    # Cap exceeded but all running → nothing evicted.
    assert store.size() == 3
    assert all(store.get(j) is not None for j in running)
    # Once the oldest finishes, the next create can reclaim it.
    store.set_done(running[0], {"ok": True})
    store.create()
    assert store.get(running[0]) is None
    assert all(store.get(j) is not None for j in running[1:])
