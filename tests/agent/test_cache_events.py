"""Feature B — agent GET /v1/cache/eviction-events.

cache-catcher appends one JSON line per sent eviction alert to a shared volume
mounted read-only into the agent. The control plane polls this endpoint (Karl's
N1: nothing on the NAS calls the orchestrator) and restarts the sweep pass at the
newest event it has not yet acted on.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

AUTH = {"Authorization": "Bearer " + "a" * 32}


def _event(ts: str, deletes: int = 12) -> str:
    return json.dumps({"ts": ts, "deletes": deletes, "window_s": 60, "actor": "other"})


def _client(path: Path) -> TestClient:
    settings = Settings(orchestrator_token="a" * 32, eviction_events_path=path)
    return TestClient(create_agent_app(settings=settings), headers=AUTH)


def test_returns_events_after_since(tmp_path):
    path = tmp_path / "eviction-events.jsonl"
    path.write_text(
        "\n".join([_event("2026-11-20T01:00:00Z"), _event("2026-11-21T02:00:00Z")]) + "\n"
    )
    r = _client(path).get("/v1/cache/eviction-events", params={"since": "2026-11-20T01:00:00Z"})
    assert r.status_code == 200
    assert [e["ts"] for e in r.json()["events"]] == ["2026-11-21T02:00:00Z"]


def test_without_since_returns_every_event(tmp_path):
    path = tmp_path / "eviction-events.jsonl"
    path.write_text(_event("2026-11-20T01:00:00Z") + "\n" + _event("2026-11-21T02:00:00Z") + "\n")
    events = _client(path).get("/v1/cache/eviction-events").json()["events"]
    assert len(events) == 2
    assert set(events[0]) == {"ts", "deletes", "window_s", "actor"}


def test_missing_file_is_an_empty_list(tmp_path):
    r = _client(tmp_path / "absent.jsonl").get("/v1/cache/eviction-events")
    assert r.status_code == 200
    assert r.json() == {"events": []}


def test_malformed_and_partial_lines_are_skipped(tmp_path):
    path = tmp_path / "eviction-events.jsonl"
    path.write_text(
        _event("2026-11-20T01:00:00Z")
        + "\nnot json at all\n"
        + json.dumps({"ts": "2026-11-20T03:00:00Z"})  # missing fields
        + "\n"
        + _event("2026-11-21T02:00:00Z")
        + '\n{"ts": "2026-11-22T0'  # partial last line, mid-write
    )
    r = _client(path).get("/v1/cache/eviction-events")
    assert r.status_code == 200
    assert [e["ts"] for e in r.json()["events"]] == [
        "2026-11-20T01:00:00Z",
        "2026-11-21T02:00:00Z",
    ]


def test_event_with_a_non_iso_ts_is_skipped(tmp_path):
    # The control plane turns ts into the pass boundary via strftime; a ts in any
    # other shape would yield NULL there, so it must never leave the agent.
    path = tmp_path / "eviction-events.jsonl"
    path.write_text(
        _event("2026-11-20 01:00:00")
        + "\n"
        + _event("yesterday")
        + "\n"
        + _event("2026-11-21T02:00:00Z")
        + "\n"
    )
    events = _client(path).get("/v1/cache/eviction-events").json()["events"]
    assert [e["ts"] for e in events] == ["2026-11-21T02:00:00Z"]


def test_caps_at_the_newest_1000_events(tmp_path):
    path = tmp_path / "eviction-events.jsonl"
    lines = [_event(f"2026-11-20T00:{i // 60:02d}:{i % 60:02d}Z") for i in range(1200)]
    path.write_text("\n".join(lines) + "\n")
    events = _client(path).get("/v1/cache/eviction-events").json()["events"]
    assert len(events) == 1000
    assert events[-1]["ts"] == "2026-11-20T00:19:59Z"
    assert events[0]["ts"] == "2026-11-20T00:03:20Z"


def test_requires_bearer(tmp_path):
    settings = Settings(orchestrator_token="a" * 32, eviction_events_path=tmp_path / "x")
    client = TestClient(create_agent_app(settings=settings))
    assert client.get("/v1/cache/eviction-events").status_code == 401


def test_missing_events_directory_is_a_503_not_an_empty_list(tmp_path):
    """Final-review I4: a missing mount must not look like "no evictions yet"; that
    would leave the whole feature silently dead after a bad compose edit."""
    r = _client(tmp_path / "not-mounted" / "eviction-events.jsonl").get("/v1/cache/eviction-events")
    assert r.status_code == 503
