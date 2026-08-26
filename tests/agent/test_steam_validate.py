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


def test_validate_includes_a_second_depot_with_differing_group_id(tmp_path):
    """A second .bin under a differing-group-id name (real Grim Dawn shape)
    must be found and counted -- proving the widened glob end-to-end through
    the actual endpoint, not just the locator in isolation."""
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    (mcache / "v1" / f"{APP}_{APP}_{DEPOT}_{GID}.bin").write_bytes(FIXTURE.read_bytes())
    depot2, group2, gid2 = 483840, 483840, 999
    (mcache / "v1" / f"{APP}_{group2}_{depot2}_{gid2}.bin").write_bytes(FIXTURE.read_bytes())

    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text(json.dumps({str(APP): [GID]}))

    cache_root = tmp_path / "lancache"
    levels, ident, slice_sz = "2:2", "steam", 10_485_760
    slice_range = slice_range_zero(slice_sz)
    for depot_id in (DEPOT, depot2):
        for sha in parse_chunk_shas(FIXTURE.read_bytes()):
            h = cache_key(ident, steam_chunk_uri(depot_id, sha), slice_range)
            p = cache_path(cache_root, h, levels)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"data")

    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=cache_root,
        cache_levels=levels,
        steam_cache_identifier=ident,
        cache_slice_size_bytes=slice_sz,
        steam_manifest_cache_dir=mcache,
        steam_prefill_config_dir=cfg,
    )
    client = TestClient(create_agent_app(settings=settings))
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})

    body = client.post("/v1/steam/validate", json={"app_id": APP}).json()
    # FIXTURE has 60 chunks; two depots fully cached -> 120 total, 120 cached.
    # Before the fix, the second depot was invisible and this would read 60/60.
    assert body["chunks_total"] == 120
    assert body["chunks_cached"] == 120
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


def test_validate_pins_to_prefilled_gid_not_newest_mtime(tmp_path):
    """Validate selects the manifest gid SteamPrefill actually prefilled (from
    successfullyDownloadedDepots.json), even when a STALE newer-mtime manifest
    for the same depot exists — the false-Partial root cause (a force prefill
    caches the current version while validate measured an older archived one)."""
    import os

    stale_gid = 9999999999999999999
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    prefilled = mcache / "v1" / f"{APP}_{APP}_{DEPOT}_{GID}.bin"
    stale = mcache / "v1" / f"{APP}_{APP}_{DEPOT}_{stale_gid}.bin"
    prefilled.write_bytes(FIXTURE.read_bytes())
    stale.write_bytes(FIXTURE.read_bytes())
    os.utime(prefilled, (1000, 1000))  # prefilled gid is OLDER
    os.utime(stale, (2000, 2000))  # stale gid is NEWER by mtime

    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text(json.dumps({str(APP): [GID]}))

    cache_root = tmp_path / "lancache"
    levels, ident, slice_sz = "2:2", "steam", 10_485_760
    slice_range = slice_range_zero(slice_sz)
    for sha in parse_chunk_shas(FIXTURE.read_bytes()):
        p = cache_path(
            cache_root, cache_key(ident, steam_chunk_uri(DEPOT, sha), slice_range), levels
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"data")

    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=cache_root,
        cache_levels=levels,
        steam_cache_identifier=ident,
        cache_slice_size_bytes=slice_sz,
        steam_manifest_cache_dir=mcache,
        steam_prefill_config_dir=cfg,
    )
    client = TestClient(create_agent_app(settings=settings))
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    body = client.post("/v1/steam/validate", json={"app_id": APP}).json()

    # The PREFILLED gid's manifest was selected (not the newer-mtime stale one).
    assert f"{DEPOT}:{GID}" in body["versions"]
    assert str(stale_gid) not in body["versions"]
    assert body["chunks_cached"] == 60


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


# --- depot-scoping: exclude never-prefilled (other-language/optional) depots ---

MD_APP, MD_GID = 800440, 9111222333444555666


def _build_multidepot(tmp_path: Path, *, depot_cached: dict[int, tuple[int, int]]) -> TestClient:
    """Build a multi-depot app from .shas manifests. ``depot_cached`` maps
    depot_id -> (n_chunks, n_cached): each depot gets ``n_chunks`` distinct 40-hex
    SHAs, the first ``n_cached`` of which are written into the on-disk cache."""
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    cache_root = tmp_path / "lancache"
    cache_root.mkdir()
    levels, ident, slice_sz = "2:2", "steam", 10_485_760
    slice_range = slice_range_zero(slice_sz)
    for depot, (n, ncached) in depot_cached.items():
        shas = [f"{depot:06x}{i:034x}" for i in range(n)]  # distinct per (depot, i)
        (mcache / "v1" / f"{MD_APP}_{MD_APP}_{depot}_{MD_GID}.shas").write_text(
            "\n".join(shas) + "\n"
        )
        for sha in shas[:ncached]:
            p = cache_path(
                cache_root, cache_key(ident, steam_chunk_uri(depot, sha), slice_range), levels
            )
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"data")
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text("{}")
    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=cache_root,
        cache_levels=levels,
        steam_cache_identifier=ident,
        cache_slice_size_bytes=slice_sz,
        steam_manifest_cache_dir=mcache,
        steam_prefill_config_dir=cfg,
    )
    client = TestClient(create_agent_app(settings=settings))
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return client


def test_validate_excludes_never_prefilled_depot(tmp_path):
    """A multi-language game: the prefilled depot is fully cached; an extra
    never-prefilled (other-language) depot has 0 cached chunks. The validator
    scopes to the prefilled depot only -> 'cached', not a perpetual 'partial'."""
    client = _build_multidepot(tmp_path, depot_cached={800441: (10, 10), 800442: (8, 0)})
    body = client.post("/v1/steam/validate", json={"app_id": MD_APP}).json()
    assert body["chunks_total"] == 10  # only the prefilled depot counts
    assert body["chunks_cached"] == 10
    assert body["outcome"] == "cached"


def test_validate_prefilled_depot_partial_still_counts(tmp_path):
    """A prefilled depot that's genuinely partial (>=1 cached, some missing) is
    NOT excluded -> the real gap stays visible; the never-prefilled depot is
    still dropped."""
    client = _build_multidepot(tmp_path, depot_cached={800441: (10, 9), 800442: (8, 0)})
    body = client.post("/v1/steam/validate", json={"app_id": MD_APP}).json()
    assert body["chunks_total"] == 10  # prefilled depot only (other-lang excluded)
    assert body["chunks_cached"] == 9
    assert body["outcome"] == "partial"


def test_validate_all_depots_unprefilled_is_missing(tmp_path):
    """If NO depot has any cached chunks, the app is genuinely not cached ->
    'missing' over the union, never a false 'cached'."""
    client = _build_multidepot(tmp_path, depot_cached={800441: (10, 0), 800442: (8, 0)})
    body = client.post("/v1/steam/validate", json={"app_id": MD_APP}).json()
    assert body["chunks_total"] == 18  # union of both depots
    assert body["chunks_cached"] == 0
    assert body["outcome"] == "missing"


def test_validate_excludes_shared_redist_depot(tmp_path):
    """A game with its OWN depot fully cached + a shared Steamworks Common
    Redistributables depot (228990) only partially cached must validate as CACHED —
    the shared redist is runtime content, not the game's own data (2026-07-04
    false-partial fix). Before this fix the partial redist depot (present>0, so not
    dropped by the never-prefilled rule) dragged the game to 'partial'."""
    client = _build_multidepot(tmp_path, depot_cached={800441: (10, 10), 228990: (8, 4)})
    body = client.post("/v1/steam/validate", json={"app_id": MD_APP}).json()
    assert body["chunks_total"] == 10  # only the game's own depot counts
    assert body["chunks_cached"] == 10
    assert body["outcome"] == "cached"
    assert "228990" not in body["versions"]  # redist depot excluded from versions


def test_validate_empty_manifest_is_error_at_the_endpoint(tmp_path):
    """A well-named but EMPTY manifest must not validate as cached — end to end.

    The unit test on _classify cannot catch this. With the redist fix in place,
    steam_validate's final return only runs when included > 0, so _classify(0, ·) is
    unreachable from the endpoint and that test now guards dead code.

    The trap: parse_shas and parse_chunk_shas NEVER raise — their own docstring says
    "malformed/unrecognized buffer yields an empty set". So parsed_ok counts any
    readable, well-named file whatever its contents, and a branch keyed on parsed_ok
    alone calls a zero-byte manifest 'cached', which maps to
    games.status='up_to_date'. That is exactly the #292 false green, on the platform
    where it actually happened live.

    The real discriminator is `versions`: the redist skip continues BEFORE
    versions.append, while a depot that parsed to nothing still appends. So
    all-redist is `parsed_ok and not versions`; an empty manifest has versions.
    """
    client = _build_multidepot(tmp_path, depot_cached={800441: (0, 0)})
    body = client.post("/v1/steam/validate", json={"app_id": MD_APP}).json()

    assert body["outcome"] == "error", (
        "an empty manifest is unreadable content, not an app with nothing to cache. "
        f"Got {body['outcome']!r} — this is #292 returning through the redist branch."
    )


def test_validate_all_redist_app_is_cached_not_error(tmp_path):
    """An app whose ONLY depots are shared redist validates as cached, not error.

    #292 made a zero-chunk result report 'error', which is right when the manifests
    could not be read. It is wrong here: these manifests parsed perfectly, and every
    depot was then deliberately excluded as shared Steamworks redistributables. The
    guard at steam.py's redist branch already says so — "the manifest still parsed
    (parsed_ok counts it) so an all-redist enumeration isn't a false error" — and
    #292 silently reversed it.

    Left as 'error' the app is re-validated every 6h forever and never resolves, and
    one caught at status 'downloading' becomes 'failed', which the sweep excludes
    from its candidate set — a dead end needing manual SQL.
    """
    client = _build_multidepot(tmp_path, depot_cached={228990: (8, 4)})
    body = client.post("/v1/steam/validate", json={"app_id": MD_APP}).json()

    assert body["outcome"] == "cached", (
        "manifests that parsed and were then excluded as shared redist are not an "
        f"unreadable manifest. Got {body['outcome']!r} / {body.get('error')!r}"
    )
    assert body["chunks_total"] == 0
    assert body["error"] is None


def test_validate_mode000_depot_counted_cached(tmp_path):
    """A depot whose chunk files EXIST on disk but are mode-000 validates as
    CACHED: mode-000 is a transient nginx-over-NFS write-race that self-heals to
    0600 in ms (audit 2026-07-02), so present, correct-size files count. The depot
    is still KEPT (present>0, not excluded as 'never prefilled')."""
    client = _build_multidepot(tmp_path, depot_cached={800441: (10, 10)})
    # Strip the owner-read bit from every (otherwise fully-cached) chunk file.
    for f in (tmp_path / "lancache").rglob("*"):
        if f.is_file():
            f.chmod(0o000)
    body = client.post("/v1/steam/validate", json={"app_id": MD_APP}).json()
    assert body["chunks_total"] == 10  # depot KEPT (files present), not excluded
    assert body["chunks_cached"] == 10  # transient mode-000 -> counted cached
    assert body["outcome"] == "cached"
