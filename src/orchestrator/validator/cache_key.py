"""Pure lancache cache-key derivation (F7).

See ``spikes/spike_a4_lancache_cache_key.md`` for the empirical verification
against the live lancache. The nginx cache key is
``md5(identifier + uri + slice_range)``; the on-disk path consumes hex
characters from the END of the md5 per the ``levels`` directive.

No I/O, no settings import — the caller supplies all config. This keeps
the derivation trivially unit-testable against golden vectors.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


def steam_chunk_uri(depot_id: int, sha_hex: str) -> str:
    """URI nginx caches a Steam depot chunk under: ``/depot/<id>/chunk/<sha>``.

    Validates inputs (path-traversal guard): ``depot_id`` must be a
    non-negative int and ``sha_hex`` 40 lowercase hex chars.
    """
    if depot_id < 0:
        raise ValueError(f"depot_id must be >= 0, got {depot_id}")
    if not _SHA_RE.match(sha_hex):
        raise ValueError(f"sha_hex must be 40 lowercase hex chars, got {sha_hex!r}")
    return f"/depot/{depot_id}/chunk/{sha_hex}"


def slice_range_zero(slice_size: int) -> str:
    """The first slice's Range value: ``bytes=0-<slice_size-1>``.

    Steam depot chunks are smaller than one slice, fetched whole (no client
    Range), so each chunk lives entirely in slice 0.
    """
    if slice_size <= 0:
        raise ValueError(f"slice_size must be > 0, got {slice_size}")
    return f"bytes=0-{slice_size - 1}"


def cache_key(identifier: str, uri: str, slice_range: str) -> str:
    """``md5(identifier + uri + slice_range)`` as 32-char lowercase hex."""
    payload = f"{identifier}{uri}{slice_range}".encode()
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


def cache_path(cache_root: Path, h: str, levels: str) -> Path:
    """nginx cache file path for md5 hex ``h`` under ``levels`` (e.g. ``"2:2"``).

    nginx consumes hex chars from the END of ``h``: for ``L1:L2:..:Ln`` the
    FIRST directory uses the final ``Ln`` chars, the next uses the ``L(n-1)``
    chars immediately before, and so on, with the full hash as the filename.
    For ``2:2``: ``<h[-2:]>/<h[-4:-2]>/<h>``.
    """
    if not _HEX32_RE.match(h):
        raise ValueError(f"expected 32-char md5 hex, got {h!r}")
    widths = [int(x) for x in levels.split(":")]
    parts: list[str] = []
    end = len(h)
    for w in widths:
        parts.append(h[end - w : end])
        end -= w
    return cache_root.joinpath(*parts, h)
