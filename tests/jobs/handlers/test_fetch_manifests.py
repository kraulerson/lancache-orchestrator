"""Tests for orchestrator.jobs.handlers.fetch_manifests."""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.fetch_manifests import fetch_manifests_handler

pytestmark = pytest.mark.asyncio


class _StubAgent:
    def __init__(self):
        self.called = False

    async def fetch_manifests(self):
        self.called = True
        return {"fetched": 7, "skipped": 1, "failed": 0, "apps": 8}


class _Deps:
    def __init__(self, agent):
        self.agent_client = agent
        self.pool = None


async def test_handler_calls_agent():
    agent = _StubAgent()
    await fetch_manifests_handler({"id": 1, "kind": "fetch_manifests"}, _Deps(agent))
    assert agent.called


async def test_handler_raises_when_agent_absent():
    with pytest.raises(ValueError):
        await fetch_manifests_handler({"id": 1}, _Deps(None))
