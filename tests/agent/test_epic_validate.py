"""Tests for the agent POST /v1/epic/validate endpoint."""

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

TOKEN = "a" * 32  # must match what _isolated_env injects as ORCH_TOKEN
IDENTS = ["ident-x", "ident-y"]
CDN_BASE = "/cdn/chunks"
SLICE_SZ = 10_485_760
LEVELS = "2:2"
VERSION = 22
N_CHUNKS = 2


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _raw_manifest() -> bytes:
    return build_manifest(VERSION, make_chunks(N_CHUNKS))


def _make_settings(tmp_path: Path, *, identifiers: list[str] = IDENTS) -> tuple[Settings, Path]:
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
    return settings, cache_root


def _make_client(tmp_path: Path, *, identifiers: list[str] = IDENTS) -> tuple[TestClient, Path]:
    settings, cache_root = _make_settings(tmp_path, identifiers=identifiers)
    app = create_agent_app(settings=settings)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return client, cache_root


def _chunk_candidate_paths(cache_root: Path, raw: bytes) -> list[list[Path]]:
    """Per-chunk list of candidate paths (one per identifier in IDENTS)."""
    m = parse_manifest(raw)
    sr = slice_range_zero(SLICE_SZ)
    result: list[list[Path]] = []
    for chunk in m.chunks:
        cp = chunk_path(chunk, m.version)
        uri = epic_chunk_uri(cp, CDN_BASE)
        result.append(
            [cache_path(cache_root, cache_key(ident, uri, sr), LEVELS) for ident in IDENTS]
        )
    return result


# ---------------------------------------------------------------------------
# (a) chunk cached under 2nd identifier ONLY — proves present-if-any logic
# ---------------------------------------------------------------------------


def test_chunk_cached_under_second_identifier_only(tmp_path):
    """A chunk cached ONLY under the 2nd identifier counts as cached —
    validate_chunks_any checks all candidates; only 1 of N_CHUNKS is placed,
    so chunks_cached == 1 and outcome == 'partial'."""
    raw = _raw_manifest()
    client, cache_root = _make_client(tmp_path)
    paths = _chunk_candidate_paths(cache_root, raw)
    # Place chunk[0] under identifier[1] ("ident-y") ONLY — not under [0]
    p = paths[0][1]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"cached")

    r = client.post(
        "/v1/epic/validate",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(raw),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["chunks_cached"] == 1
    assert body["chunks_total"] == N_CHUNKS
    assert body["chunks_missing"] == N_CHUNKS - 1
    assert body["outcome"] == "partial"
    assert body["error"] is None


# ---------------------------------------------------------------------------
# (b) No chunks cached — outcome "missing"
# ---------------------------------------------------------------------------


def test_all_chunks_absent(tmp_path):
    """No cache files on disk → chunks_cached == 0, outcome 'missing'."""
    raw = _raw_manifest()
    client, _ = _make_client(tmp_path)
    body = client.post(
        "/v1/epic/validate",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(raw),
        },
    ).json()
    assert body["chunks_total"] == N_CHUNKS
    assert body["chunks_cached"] == 0
    assert body["outcome"] == "missing"
    assert body["error"] is None


# ---------------------------------------------------------------------------
# (c) Empty epic_cache_identifiers → error "no_epic_identifiers"
# ---------------------------------------------------------------------------


def test_empty_identifiers_returns_error(tmp_path):
    """If epic_cache_identifiers is empty the endpoint returns an error outcome
    rather than raising (no 500)."""
    client, _ = _make_client(tmp_path, identifiers=[])
    raw = _raw_manifest()
    body = client.post(
        "/v1/epic/validate",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(raw),
        },
    ).json()
    assert body["outcome"] == "error"
    assert body["error"] == "no_epic_identifiers"


# ---------------------------------------------------------------------------
# (d) Malformed manifest bytes → outcome "error" (no 500)
# ---------------------------------------------------------------------------


def test_malformed_manifest_returns_error(tmp_path):
    """Garbage base64 payload returns an error outcome — the endpoint must
    never propagate EpicManifestError as an unhandled exception."""
    client, _ = _make_client(tmp_path)
    body = client.post(
        "/v1/epic/validate",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(b"not-a-real-manifest\xff\x00\xde\xad"),
        },
    ).json()
    assert body["outcome"] == "error"
    assert body["error"] == "manifest_parse_failed"


# ---------------------------------------------------------------------------
# (e-new) All chunks present under first identifier → "cached"
# ---------------------------------------------------------------------------


def test_all_chunks_cached(tmp_path):
    """When every unique chunk is cached under the 1st identifier the outcome is
    'cached' and chunks_cached == chunks_total (N_CHUNKS)."""
    raw = _raw_manifest()
    client, cache_root = _make_client(tmp_path)
    paths = _chunk_candidate_paths(cache_root, raw)
    # Place ALL chunks under identifier[0] ("ident-x")
    for cands in paths:
        p = cands[0]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"cached")

    body = client.post(
        "/v1/epic/validate",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(raw),
        },
    ).json()
    assert body["outcome"] == "cached"
    assert body["chunks_total"] == N_CHUNKS
    assert body["chunks_cached"] == N_CHUNKS
    assert body["chunks_missing"] == 0
    assert body["error"] is None


# ---------------------------------------------------------------------------
# (f) Zero-chunk manifest → chunks_total == 0, outcome "cached"
# ---------------------------------------------------------------------------


def test_zero_chunk_manifest(tmp_path):
    """A manifest with no chunks yields chunks_total == 0 and outcome 'cached' —
    there is nothing to validate, so the game is trivially fully cached."""
    raw = build_manifest(VERSION, make_chunks(0))
    client, _ = _make_client(tmp_path)

    body = client.post(
        "/v1/epic/validate",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(raw),
        },
    ).json()
    assert body["chunks_total"] == 0
    assert body["outcome"] == "cached"
    assert body["error"] is None


# ---------------------------------------------------------------------------
# (e) 401 without bearer token
# ---------------------------------------------------------------------------


def test_no_bearer_returns_401(tmp_path):
    """Requests without a bearer token must be rejected with HTTP 401,
    matching the agent-level BearerAuthMiddleware (mirrors test_steam_validate)."""
    settings, _ = _make_settings(tmp_path)
    app = create_agent_app(settings=settings)
    client = TestClient(app)  # intentionally no Authorization header
    r = client.post(
        "/v1/epic/validate",
        json={
            "app_id": 1234,
            "version": str(VERSION),
            "cdn_base": CDN_BASE,
            "raw_manifest_b64": _b64(_raw_manifest()),
        },
    )
    assert r.status_code == 401
