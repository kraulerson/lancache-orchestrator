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


def test_validate_skips_corrupt_bin_keeps_valid(tmp_path):
    """COR-1 (review 2026-06-23): a corrupt/foreign .bin in the cache (here a
    non-numeric depot field that crashes int(parts[2])) must be skipped, not 500
    the whole request. The valid manifest still validates normally."""
    client = _build(tmp_path, cache_all=True)
    # Drop a corrupt sibling .bin for the SAME app, different (bad) depot field.
    mcache_v1 = tmp_path / "spcache" / "v1"
    (mcache_v1 / f"{APP}_{APP}_NOTANINT_{GID}.bin").write_bytes(b"\x00\x01garbage")
    r = client.post("/v1/steam/validate", json={"app_id": APP})
    assert r.status_code == 200, r.text
    body = r.json()
    # The good manifest's 60 chunks still validate; the corrupt bin is ignored.
    assert body["chunks_total"] == 60
    assert body["chunks_cached"] == 60
    assert body["outcome"] == "cached"


def test_validate_all_bins_corrupt_is_error_not_500(tmp_path):
    """COR-1: when manifests EXIST but none can be parsed, return a graceful
    error outcome (not 'cached' and not HTTP 500)."""
    client = _build(tmp_path, cache_all=False)
    mcache_v1 = tmp_path / "spcache" / "v1"
    other = 222222
    (mcache_v1 / f"{other}_{other}_BADDEPOT_{GID}.bin").write_bytes(b"\xff\xffnope")
    r = client.post("/v1/steam/validate", json={"app_id": other})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chunks_total"] == 0
    assert body["outcome"] == "error"
    assert body["error"]


# --- .shas sidecar manifests (apps SteamPrefill never cached) ---

SHAS_APP, SHAS_DEPOT, SHAS_GID = 700330, 700331, 8123456789012345678
_SHAS_CHUNKS = [f"{i:040x}" for i in range(1, 13)]  # 12 distinct lowercase 40-hex SHAs


def _build_shas(tmp_path: Path, *, cache_all: bool) -> TestClient:
    """Like _build, but the only manifest for SHAS_APP is a .shas sidecar."""
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    (mcache / "v1" / f"{SHAS_APP}_{SHAS_APP}_{SHAS_DEPOT}_{SHAS_GID}.shas").write_text(
        "\n".join(_SHAS_CHUNKS) + "\n"
    )
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text("{}")

    cache_root = tmp_path / "lancache"
    levels, ident, slice_sz = "2:2", "steam", 10_485_760
    if cache_all:
        slice_range = slice_range_zero(slice_sz)
        for sha in _SHAS_CHUNKS:
            h = cache_key(ident, steam_chunk_uri(SHAS_DEPOT, sha), slice_range)
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


def test_validate_shas_all_cached(tmp_path):
    """A .shas-backed app (no .bin) validates against a real cache outcome —
    NOT 'no_manifest_in_cache'."""
    client = _build_shas(tmp_path, cache_all=True)
    r = client.post("/v1/steam/validate", json={"app_id": SHAS_APP})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chunks_total"] == len(_SHAS_CHUNKS)
    assert body["chunks_cached"] == len(_SHAS_CHUNKS)
    assert body["chunks_missing"] == 0
    assert body["outcome"] == "cached"
    assert body["error"] is None
    assert body["versions"] == f"{SHAS_DEPOT}:{SHAS_GID}"


def test_validate_shas_all_missing(tmp_path):
    client = _build_shas(tmp_path, cache_all=False)
    body = client.post("/v1/steam/validate", json={"app_id": SHAS_APP}).json()
    assert body["chunks_total"] == len(_SHAS_CHUNKS)
    assert body["chunks_cached"] == 0
    assert body["outcome"] == "missing"
