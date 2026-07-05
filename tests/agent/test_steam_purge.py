"""Tests for the agent POST /v1/steam/purge endpoint (F18)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "a" * 32
APP, DEPOT, GID = 700330, 700331, 8123456789012345678
CHUNKS = [f"{i:040x}" for i in range(1, 6)]  # 5 distinct 40-hex SHAs
LEVELS, IDENT, SLICE = "2:2", "steam", 10_485_760
CHUNK_BODY = b"chunkdata"  # 9 bytes


def _seed(tmp_path: Path) -> tuple[TestClient, list[Path], Path]:
    """Seed a .shas manifest for APP + write its chunk files into the cache.
    Returns (client, chunk_files, outsider) — outsider is a file OUTSIDE the
    cache tree that a correct purge must never touch."""
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    (mcache / "v1" / f"{APP}_{APP}_{DEPOT}_{GID}.shas").write_text("\n".join(CHUNKS) + "\n")
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text("{}")

    cache_root = tmp_path / "lancache"
    slice_range = slice_range_zero(SLICE)
    chunk_files: list[Path] = []
    for sha in CHUNKS:
        h = cache_key(IDENT, steam_chunk_uri(DEPOT, sha), slice_range)
        p = cache_path(cache_root, h, LEVELS)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(CHUNK_BODY)
        chunk_files.append(p)

    outsider = tmp_path / "outside.txt"
    outsider.write_bytes(b"keep me")

    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=cache_root,
        cache_levels=LEVELS,
        steam_cache_identifier=IDENT,
        cache_slice_size_bytes=SLICE,
        steam_manifest_cache_dir=mcache,
        steam_prefill_config_dir=cfg,
    )
    client = TestClient(create_agent_app(settings=settings))
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return client, chunk_files, outsider


def test_steam_purge_deletes_enumerated_chunks(tmp_path):
    client, chunk_files, outsider = _seed(tmp_path)
    assert all(p.exists() for p in chunk_files)

    r = client.post("/v1/steam/purge", json={"app_id": APP})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == len(CHUNKS)
    assert body["failed"] == 0
    assert body["bytes_freed"] == len(CHUNKS) * len(CHUNK_BODY)
    assert all(not p.exists() for p in chunk_files)
    assert outsider.exists()  # a file outside the cache tree is never touched


def test_steam_purge_idempotent_already_gone(tmp_path):
    client, chunk_files, _ = _seed(tmp_path)
    for p in chunk_files:
        p.unlink()  # nothing cached now
    r = client.post("/v1/steam/purge", json={"app_id": APP})
    assert r.status_code == 200
    assert r.json() == {"deleted": 0, "failed": 0, "bytes_freed": 0}


def test_steam_purge_no_manifest_returns_zero(tmp_path):
    """No manifest for the app → nothing to enumerate → {deleted:0}, not an error."""
    client, _, _ = _seed(tmp_path)
    r = client.post("/v1/steam/purge", json={"app_id": 999999})
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


def test_steam_purge_requires_auth(tmp_path):
    client, chunk_files, _ = _seed(tmp_path)
    r = client.post(
        "/v1/steam/purge", json={"app_id": APP}, headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code in (401, 403)
    assert all(p.exists() for p in chunk_files)  # rejected request deleted nothing


def test_steam_purge_bad_app_id(tmp_path):
    client, _, _ = _seed(tmp_path)
    r = client.post("/v1/steam/purge", json={"app_id": -5})
    assert r.status_code == 422
