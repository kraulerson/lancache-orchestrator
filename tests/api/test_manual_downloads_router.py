"""Tests for the orchestrator GET /api/v1/manual-downloads/{launcher} proxy (#222)."""

from __future__ import annotations

import pytest

VALID_TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


class _FakeAgent:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls: list[str] = []

    async def manual_downloads(self, launcher: str):
        self.calls.append(launcher)
        if self._exc is not None:
            raise self._exc
        return self._result


async def test_proxies_agent_listing(client, unit_app):
    unit_app.state.agent_client = _FakeAgent(
        result={"launcher": "GOG", "present": True, "entries": ["trine_2", "portal"]}
    )
    r = await client.get("/api/v1/manual-downloads/GOG", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"launcher": "GOG", "present": True, "entries": ["trine_2", "portal"]}
    assert unit_app.state.agent_client.calls == ["GOG"]


async def test_invalid_launcher_400(client, unit_app):
    unit_app.state.agent_client = _FakeAgent(result={})
    # A dot is not allowed (traversal-safe allowlist) — rejected before the agent.
    r = await client.get("/api/v1/manual-downloads/a.b", headers=AUTH)
    assert r.status_code == 400
    assert unit_app.state.agent_client.calls == []  # never reached the agent


async def test_no_agent_configured_503(client, unit_app):
    # unit_app has no agent_client on state.
    if hasattr(unit_app.state, "agent_client"):
        del unit_app.state.agent_client
    r = await client.get("/api/v1/manual-downloads/GOG", headers=AUTH)
    assert r.status_code == 503


async def test_agent_error_503(client, unit_app):
    unit_app.state.agent_client = _FakeAgent(exc=RuntimeError("agent down"))
    r = await client.get("/api/v1/manual-downloads/GOG", headers=AUTH)
    assert r.status_code == 503


async def test_missing_launcher_present_false(client, unit_app):
    unit_app.state.agent_client = _FakeAgent(
        result={"launcher": "Humble", "present": False, "entries": []}
    )
    r = await client.get("/api/v1/manual-downloads/Humble", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["present"] is False


@pytest.mark.parametrize("path", ["/api/v1/manual-downloads/GOG"])
async def test_requires_auth(client, path):
    assert (await client.get(path)).status_code == 401
