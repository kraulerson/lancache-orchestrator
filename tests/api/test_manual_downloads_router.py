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
        self.include_files_calls: list[bool] = []

    async def manual_downloads(self, launcher: str, include_files: bool = False):
        self.calls.append(launcher)
        self.include_files_calls.append(include_files)
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
    # Dots/spaces ARE allowed now (Itch.io / Amazon Games); a char outside the
    # allowlist (e.g. '@') is still rejected before the agent.
    r = await client.get("/api/v1/manual-downloads/a@b", headers=AUTH)
    assert r.status_code == 400
    assert unit_app.state.agent_client.calls == []  # never reached the agent


async def test_accepts_space_launcher_and_forwards_include_files(client, unit_app):
    unit_app.state.agent_client = _FakeAgent(
        result={"launcher": "Amazon Games", "present": True, "entries": ["A Game"]}
    )
    r = await client.get("/api/v1/manual-downloads/Amazon Games", headers=AUTH)
    assert r.status_code == 200
    r2 = await client.get("/api/v1/manual-downloads/Itch.io?include_files=true", headers=AUTH)
    assert r2.status_code == 200
    agent = unit_app.state.agent_client
    assert agent.calls == ["Amazon Games", "Itch.io"]
    assert agent.include_files_calls == [False, True]


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
