"""Tests for the agent /v1/stat endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings
from orchestrator.validator.cache_key import cache_key, cache_path

if TYPE_CHECKING:
    from pathlib import Path


def _settings(cache_root: Path, **kw) -> Settings:
    return Settings(
        orchestrator_token="a" * 32,
        lancache_nginx_cache_path=cache_root,
        cache_levels="2:2",
        **kw,
    )


def _make_cached_file(cache_root: Path, h: str) -> None:
    p = cache_path(cache_root, h, "2:2")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"cached-bytes")  # size>0, owner-read bit set by default


def test_stat_counts_cached_and_missing(tmp_path):
    cached_h = cache_key("steam", "/present", "bytes=0-0")
    missing_h = cache_key("steam", "/absent", "bytes=0-0")
    _make_cached_file(tmp_path, cached_h)

    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    resp = client.post("/v1/stat", json={"hashes": [cached_h, missing_h]})
    assert resp.status_code == 200
    assert resp.json() == {"cached": 1, "missing": 1}


def test_stat_rejects_non_hex_hash(tmp_path):
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    resp = client.post("/v1/stat", json={"hashes": ["not-a-32-hex-hash"]})
    assert resp.status_code == 400


def test_stat_empty(tmp_path):
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    resp = client.post("/v1/stat", json={"hashes": []})
    assert resp.json() == {"cached": 0, "missing": 0}
