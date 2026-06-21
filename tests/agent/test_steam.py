"""Tests for the agent /v1/steam/* endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings
from orchestrator.platform.steam.prefill_driver import PrefillResult, SteamAuthStatus


class _FakeDriver:
    def __init__(self):
        self.calls = []

    async def prefill_apps(self, app_ids, *, force=False):
        self.calls.append((app_ids, force))
        return PrefillResult(ok=True, raw="OK done")

    def downloaded_state(self):
        return {440: [111, 222]}

    def auth_status(self):
        return SteamAuthStatus(ok=True)


def _client(driver) -> TestClient:
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.prefill_driver = driver
    return TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})


def test_steam_prefill_runs_to_done():
    driver = _FakeDriver()
    client = _client(driver)
    resp = client.post("/v1/steam/prefill", json={"app_ids": [440], "force": False})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    for _ in range(50):
        snap = client.get(f"/v1/steam/prefill/{job_id}").json()
        if snap["state"] == "done":
            break
    assert snap["state"] == "done"
    assert snap["result"] == {"ok": True, "raw": "OK done"}
    assert driver.calls == [([440], False)]


def test_downloaded_state():
    client = _client(_FakeDriver())
    resp = client.get("/v1/steam/downloaded-state")
    assert resp.status_code == 200
    assert resp.json() == {"440": [111, 222]}


def test_auth_status():
    client = _client(_FakeDriver())
    resp = client.get("/v1/steam/auth-status")
    assert resp.json() == {"ok": True, "reason": ""}


def test_steam_prefill_rejects_negative_app_id():
    client = _client(_FakeDriver())
    resp = client.post("/v1/steam/prefill", json={"app_ids": [-5], "force": False})
    assert resp.status_code == 422
