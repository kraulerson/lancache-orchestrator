"""Tests for the agent POST /v1/epic/purge endpoint (F18)."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings
from orchestrator.platform.epic.manifest import chunk_path, parse_manifest
from orchestrator.validator.cache_key import cache_key, cache_path, epic_chunk_uri, slice_range_zero
from tests.platform.epic._manifest_fixtures import build_manifest, make_chunks

TOKEN = "a" * 32
IDENTS = ["ident-x", "ident-y"]
CDN_BASE = "/cdn/chunks"
SLICE_SZ = 10_485_760
LEVELS = "2:2"
VERSION = 22
N_CHUNKS = 3
BODY = b"chunkbytes"  # 10 bytes


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _raw() -> bytes:
    return build_manifest(VERSION, make_chunks(N_CHUNKS))


def _client(tmp_path: Path, *, identifiers: list[str] = IDENTS) -> tuple[TestClient, Path]:
    cache_root = tmp_path / "lancache"
    cache_root.mkdir(exist_ok=True)
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "Config"
    cfg.mkdir(exist_ok=True)
    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=str(cache_root),
        cache_levels=LEVELS,
        cache_slice_size_bytes=SLICE_SZ,
        epic_cache_identifiers=identifiers,
        steam_manifest_cache_dir=str(mcache),
        steam_prefill_config_dir=str(cfg),
    )
    client = TestClient(create_agent_app(settings=settings))
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return client, cache_root


def _candidate_paths(cache_root: Path, raw: bytes) -> list[list[Path]]:
    m = parse_manifest(raw)
    sr = slice_range_zero(SLICE_SZ)
    out: list[list[Path]] = []
    for chunk in m.chunks:
        cp = chunk_path(chunk, m.version)
        uri = epic_chunk_uri(cp, CDN_BASE)
        out.append([cache_path(cache_root, cache_key(ident, uri, sr), LEVELS) for ident in IDENTS])
    return out


def _post_purge(client: TestClient, raw: bytes) -> object:
    return client.post(
        "/v1/epic/purge",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(raw),
        },
    )


def test_epic_purge_deletes_cached_chunks(tmp_path):
    raw = _raw()
    client, cache_root = _client(tmp_path)
    placed: list[Path] = []
    for cands in _candidate_paths(cache_root, raw):  # place each chunk under ident[0]
        p = cands[0]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(BODY)
        placed.append(p)
    outsider = tmp_path / "outside.txt"
    outsider.write_bytes(b"keep")

    r = _post_purge(client, raw)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == N_CHUNKS
    assert body["failed"] == 0
    assert body["bytes_freed"] == N_CHUNKS * len(BODY)
    assert all(not p.exists() for p in placed)
    assert outsider.exists()  # file outside the cache tree untouched


def test_epic_purge_deletes_all_candidate_identifiers(tmp_path):
    """A chunk cached under BOTH identifiers → purge removes both candidate files
    (we don't know which CDN host served it, so delete every candidate)."""
    raw = _raw()
    client, cache_root = _client(tmp_path)
    both = _candidate_paths(cache_root, raw)[0]  # chunk 0's two candidate paths
    for p in both:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(BODY)

    body = _post_purge(client, raw).json()

    assert body["deleted"] == 2  # both identifiers' files for the one chunk
    assert all(not p.exists() for p in both)


def test_epic_purge_idempotent_nothing_cached(tmp_path):
    raw = _raw()
    client, _ = _client(tmp_path)
    r = _post_purge(client, raw)
    assert r.status_code == 200
    assert r.json() == {"deleted": 0, "failed": 0, "bytes_freed": 0}


def test_epic_purge_malformed_manifest_returns_zero(tmp_path):
    """Garbage manifest → {deleted:0}, never a 500 (nothing enumerable to delete)."""
    client, _ = _client(tmp_path)
    r = client.post(
        "/v1/epic/purge",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(b"not-a-real-manifest\xff\x00\xde\xad"),
        },
    )
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


def test_epic_purge_no_identifiers_returns_zero(tmp_path):
    raw = _raw()
    client, _ = _client(tmp_path, identifiers=[])
    r = _post_purge(client, raw)
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


def test_epic_purge_no_bearer_returns_401(tmp_path):
    cache_root = tmp_path / "lancache"
    cache_root.mkdir()
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    cfg = tmp_path / "Config"
    cfg.mkdir()
    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=str(cache_root),
        cache_levels=LEVELS,
        cache_slice_size_bytes=SLICE_SZ,
        epic_cache_identifiers=IDENTS,
        steam_manifest_cache_dir=str(mcache),
        steam_prefill_config_dir=str(cfg),
    )
    client = TestClient(create_agent_app(settings=settings))  # no auth header
    r = _post_purge(client, _raw())
    assert r.status_code == 401
