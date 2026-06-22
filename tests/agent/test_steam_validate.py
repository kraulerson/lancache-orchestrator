"""Tests for the agent POST /v1/steam/validate endpoint."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.agent.manifest_parser import parse_chunk_shas
from orchestrator.core.settings import Settings
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_manifest.bin"
TOKEN = "a" * 32
APP, DEPOT, GID = 1018130, 1018131, 2926834372583665729


def _build(tmp_path: Path, *, cache_all: bool) -> TestClient:
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    (mcache / "v1" / f"{APP}_{APP}_{DEPOT}_{GID}.bin").write_bytes(FIXTURE.read_bytes())
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text(json.dumps({str(APP): [GID]}))

    cache_root = tmp_path / "lancache"
    levels, ident, slice_sz = "2:2", "steam", 10_485_760
    if cache_all:
        slice_range = slice_range_zero(slice_sz)
        for sha in parse_chunk_shas(FIXTURE.read_bytes()):
            h = cache_key(ident, steam_chunk_uri(DEPOT, sha), slice_range)
            p = cache_path(cache_root, h, levels)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"data")
    else:
        cache_root.mkdir()

    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=cache_root,
        cache_levels=levels,
        steam_cache_identifier=ident,
        cache_slice_size_bytes=slice_sz,
        steam_manifest_cache_dir=mcache,
        steam_prefill_config_dir=cfg,
    )
    app = create_agent_app(settings=settings)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return client


def test_validate_all_cached(tmp_path):
    client = _build(tmp_path, cache_all=True)
    r = client.post("/v1/steam/validate", json={"app_id": APP})
    assert r.status_code == 200
    body = r.json()
    assert body["chunks_total"] == 60
    assert body["chunks_cached"] == 60
    assert body["chunks_missing"] == 0
    assert body["outcome"] == "cached"


def test_validate_all_missing(tmp_path):
    client = _build(tmp_path, cache_all=False)
    body = client.post("/v1/steam/validate", json={"app_id": APP}).json()
    assert body["chunks_total"] == 60
    assert body["chunks_cached"] == 0
    assert body["outcome"] == "missing"


def test_validate_no_manifest(tmp_path):
    client = _build(tmp_path, cache_all=False)
    body = client.post("/v1/steam/validate", json={"app_id": 999999}).json()
    assert body["chunks_total"] == 0
    assert body["outcome"] == "error"
    assert "no_manifest" in body["error"]


def test_validate_bad_app_id(tmp_path):
    client = _build(tmp_path, cache_all=False)
    r = client.post("/v1/steam/validate", json={"app_id": -5})
    assert r.status_code == 422
