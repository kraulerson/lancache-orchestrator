"""Tests for the agent /v1/pull endpoint."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings

SHA = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"


def _settings(**kw) -> Settings:
    return Settings(orchestrator_token="a" * 32, **kw)


def _client(monkeypatch, handler) -> TestClient:
    monkeypatch.setattr(
        "orchestrator.agent.puller._build_transport",
        lambda: httpx.MockTransport(handler),
    )
    app = create_agent_app(settings=_settings())
    return TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})


def test_pull_runs_to_done(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x" * 8)

    client = _client(monkeypatch, handler)
    resp = client.post(
        "/v1/pull",
        json={
            "chunks": [{"url": f"/depot/1/chunk/{SHA}", "host": "lancache.steamcontent.com"}],
            "user_agent": "UA/1.0",
        },
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    for _ in range(50):
        snap = client.get(f"/v1/pull/{job_id}").json()
        if snap["state"] == "done":
            break
    assert snap["state"] == "done"
    assert snap["result"]["chunks_ok"] == 1
    assert snap["result"]["chunks_failed"] == 0


@pytest.mark.parametrize(
    "bad_url",
    ["http://evil.com/x", "//evil.com/x", "/depot/../../etc/passwd", "user@host/x", ""],
)
def test_pull_rejects_ssrf_urls(monkeypatch, bad_url):
    def handler(request):  # must never be called
        raise AssertionError("transport must not be hit for a rejected URL")

    client = _client(monkeypatch, handler)
    resp = client.post(
        "/v1/pull",
        json={"chunks": [{"url": bad_url, "host": "h"}], "user_agent": "UA/1.0"},
    )
    assert resp.status_code == 400


def test_pull_unknown_job_404(monkeypatch):
    def handler(request):
        return httpx.Response(200)

    client = _client(monkeypatch, handler)
    assert client.get("/v1/pull/nope").status_code == 404
