"""Tests for orchestrator.validator.cache_key (F7).

Golden vectors are real cached chunks from the live lancache, verified in
spikes/spike_a4_lancache_cache_key.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)

SHA = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"


def test_golden_vector_full_chain():
    uri = steam_chunk_uri(529345, SHA)
    assert uri == f"/depot/529345/chunk/{SHA}"
    h = cache_key("steam", uri, slice_range_zero(10_485_760))
    assert h == "22e7d56f787714bc78e23495d93da0db"
    p = cache_path(Path("/data/cache/cache"), h, "2:2")
    assert p == Path("/data/cache/cache/db/a0/22e7d56f787714bc78e23495d93da0db")


@pytest.mark.parametrize(
    "sha,expected_md5",
    [
        ("c8e5d44ca8618200552eb754ff6f6922c92a54ff", "22e7d56f787714bc78e23495d93da0db"),
        ("234a47ed3005727db220987ecac460030295bd79", "c083a3b195ee7992b4df83b4488a9791"),
        ("dbff8764f904bf6dc6b98cb001996a407b79f15e", "cccaab923f4242ac691d701331a26129"),
    ],
)
def test_golden_vectors_depot_529345(sha, expected_md5):
    """All three real HIT chunks from the access log reproduce exactly."""
    uri = steam_chunk_uri(529345, sha)
    assert cache_key("steam", uri, "bytes=0-10485759") == expected_md5


def test_slice_range_zero():
    assert slice_range_zero(10_485_760) == "bytes=0-10485759"
    assert slice_range_zero(1_048_576) == "bytes=0-1048575"
    with pytest.raises(ValueError):
        slice_range_zero(0)


def test_levels_generalization():
    h = "0123456789abcdef0123456789abcdef"
    assert cache_path(Path("/c"), h, "2:2") == Path(f"/c/ef/cd/{h}")
    assert cache_path(Path("/c"), h, "1:2") == Path(f"/c/f/de/{h}")
    assert cache_path(Path("/c"), h, "1:1:1") == Path(f"/c/f/e/d/{h}")


def test_cache_path_rejects_bad_hash():
    with pytest.raises(ValueError):
        cache_path(Path("/c"), "NOTHEX", "2:2")
    with pytest.raises(ValueError):
        cache_path(Path("/c"), "abc", "2:2")  # too short


def test_cache_path_rejects_bad_levels():
    """Bug A: levels that would slice past the 32-char digest or use a
    zero/negative width must raise, not silently produce wrong paths."""
    h = "0123456789abcdef0123456789abcdef"
    with pytest.raises(ValueError):
        cache_path(Path("/c"), h, "0")  # zero width
    with pytest.raises(ValueError):
        cache_path(Path("/c"), h, "99")  # exceeds digest length
    with pytest.raises(ValueError):
        cache_path(Path("/c"), h, "2:" * 16 + "2")  # 17 levels, sum 34 > 32
    with pytest.raises(ValueError):
        cache_path(Path("/c"), h, "30:4")  # sum 34 > 32
    with pytest.raises(ValueError):
        cache_path(Path("/c"), h, "")  # empty


def test_rejects_bad_sha():
    with pytest.raises(ValueError):
        steam_chunk_uri(1, "NOTHEX")
    with pytest.raises(ValueError):
        steam_chunk_uri(1, "abc")  # too short
    with pytest.raises(ValueError):
        steam_chunk_uri(1, SHA.upper())  # uppercase not allowed


def test_rejects_negative_depot():
    with pytest.raises(ValueError):
        steam_chunk_uri(-1, SHA)


def test_cache_path_always_under_root():
    """Bug E / D8: the computed path must stay under the cache root."""
    root = Path("/data/cache/cache")
    h = "22e7d56f787714bc78e23495d93da0db"
    p = cache_path(root, h, "2:2")
    assert p.is_relative_to(root)


def test_epic_chunk_uri_shape():
    """F6 staged helper for the deferred F7-Epic disk-stat validator."""
    from orchestrator.validator.cache_key import epic_chunk_uri

    assert epic_chunk_uri("ChunksV5/00/a_b.chunk", "/base") == "/base/ChunksV5/00/a_b.chunk"
    assert epic_chunk_uri("ChunksV5/00/a.chunk", "/base/") == "/base/ChunksV5/00/a.chunk"
