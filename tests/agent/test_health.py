"""Tests for the agent /v1/health liveness endpoint (re-arch ④ Phase 0).

The agent owns the lancache cache mount, so its liveness probe also reports its
local validator self-test result. Liveness stays 200 either way; the control
plane (running on an LXC with no cache mount) reads `validator_healthy` to gate
its own `app.state.validator_healthy`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


def _settings(cache_root: Path) -> Settings:
    return Settings(
        orchestrator_token="a" * 32,
        lancache_nginx_cache_path=cache_root,
        cache_levels="2:2",
    )


def test_health_validator_healthy_true_when_cache_present(tmp_path):
    (tmp_path / "ab").mkdir()  # non-empty cache dir → self-test passes
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app)
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["validator_healthy"] is True


def test_health_still_200_but_validator_unhealthy_when_cache_missing(tmp_path):
    app = create_agent_app(settings=_settings(tmp_path / "nope"))
    client = TestClient(app)
    resp = client.get("/v1/health")
    assert resp.status_code == 200  # liveness is unaffected by validator state
    body = resp.json()
    assert body["ok"] is True
    assert body["validator_healthy"] is False
