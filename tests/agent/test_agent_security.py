"""Agent auth + allowlist + boot-guard tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator.agent.app import _enforce_agent_lan_bind_policy, create_agent_app
from orchestrator.core.settings import Settings

TOKEN = "a" * 32


def _app(**settings_kw):
    return create_agent_app(settings=Settings(orchestrator_token=TOKEN, **settings_kw))


def test_health_is_exempt():
    client = TestClient(_app())
    assert client.get("/v1/health").status_code == 200


def test_pull_requires_bearer():
    client = TestClient(_app())
    resp = client.post("/v1/pull", json={"chunks": [], "user_agent": "UA/1.0"})
    assert resp.status_code == 401


def test_pull_accepts_valid_bearer():
    client = TestClient(_app())
    resp = client.post(
        "/v1/pull",
        json={"chunks": [], "user_agent": "UA/1.0"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 202


def test_boot_guard_refuses_non_loopback_without_allowlist():
    s = Settings(orchestrator_token=TOKEN, agent_bind_host="0.0.0.0")  # noqa: S104
    with pytest.raises(SystemExit):
        _enforce_agent_lan_bind_policy(s)


def test_boot_guard_allows_non_loopback_with_allowlist(monkeypatch):
    monkeypatch.setenv("ORCH_ALLOWED_SOURCE_IPS", "10.0.0.0/24")
    s = Settings(orchestrator_token=TOKEN, agent_bind_host="0.0.0.0")  # noqa: S104
    _enforce_agent_lan_bind_policy(s)  # must NOT raise


def test_main_module_exposes_app_factory():
    import orchestrator.agent.__main__ as m

    assert hasattr(m, "main")
