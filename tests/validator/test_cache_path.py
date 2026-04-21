"""Regression tests for Lancache cache path computation.

Validates the formula against a known-good key extracted from the real
Lancache deployment at 192.168.1.40 (Spike C, 2026-04-20).
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def compute_cache_path(
    cache_root: Path,
    cache_identifier: str,
    uri: str,
    slice_range: str,
) -> tuple[str, Path]:
    """Compute nginx cache key and on-disk path (levels=2:2)."""
    cache_key = f"{cache_identifier}{uri}{slice_range}"
    # nosemgrep: insecure-hash-algorithm-md5 — nginx uses md5 for cache keys
    md5_hex = hashlib.md5(cache_key.encode()).hexdigest()  # noqa: S324
    disk_path = cache_root / md5_hex[-2:] / md5_hex[-4:-2] / md5_hex
    return cache_key, disk_path


def test_cache_path_matches_real_lancache_deployment() -> None:
    """Known-good cache key extracted from live Lancache nginx cache file.

    KEY line from /lancache/lancache/cache/cache/3b/3b/304f9746b57b02228e64a57a8d283b3b:
        steam/depot/292732/chunk/d3320b3718cea87ecf790ef29eb09ee6342fce0ebytes=0-10485759
    """
    cache_root = Path("/data/cache/cache")
    key, path = compute_cache_path(
        cache_root,
        "steam",
        "/depot/292732/chunk/d3320b3718cea87ecf790ef29eb09ee6342fce0e",
        "bytes=0-10485759",
    )

    expected_key = (
        "steam/depot/292732/chunk/d3320b3718cea87ecf790ef29eb09ee6342fce0ebytes=0-10485759"
    )
    assert key == expected_key
    assert path == Path("/data/cache/cache/3b/3b/304f9746b57b02228e64a57a8d283b3b")


def test_slice_size_10mib_boundaries() -> None:
    """Slice ranges use 10 MiB (10,485,760 byte) boundaries, not 1 MiB."""
    slice_size = 10_485_760
    first_range = f"bytes=0-{slice_size - 1}"
    second_range = f"bytes={slice_size}-{2 * slice_size - 1}"

    assert first_range == "bytes=0-10485759"
    assert second_range == "bytes=10485760-20971519"


def test_levels_2_2_directory_structure() -> None:
    """nginx levels=2:2 maps md5[-2:] / md5[-4:-2] / md5."""
    cache_root = Path("/cache")
    md5_hex = "304f9746b57b02228e64a57a8d283b3b"
    expected = Path(f"/cache/{md5_hex[-2:]}/{md5_hex[-4:-2]}/{md5_hex}")
    assert expected == Path("/cache/3b/3b/304f9746b57b02228e64a57a8d283b3b")

    _, path = compute_cache_path(
        cache_root,
        "steam",
        "/depot/292732/chunk/d3320b3718cea87ecf790ef29eb09ee6342fce0e",
        "bytes=0-10485759",
    )
    assert path == expected


def test_epic_cache_identifier_is_epicgames() -> None:
    """Epic cache identifier is 'epicgames' (hostname map), not raw CDN hostname."""
    cache_root = Path("/cache")
    key, _ = compute_cache_path(
        cache_root,
        "epicgames",
        "/Builds/Fortnite/CloudDir/ChunksV4/07/001122334455_AABBCCDD.chunk",
        "bytes=0-10485759",
    )
    assert key.startswith("epicgames/")
