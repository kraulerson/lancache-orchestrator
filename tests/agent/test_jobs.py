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
