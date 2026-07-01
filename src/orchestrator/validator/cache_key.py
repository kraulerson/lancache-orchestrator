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


def epic_chunk_uri(chunk_path: str, cdn_base_path: str) -> str:
    """Build the URI nginx caches an Epic chunk under: ``<cdn_base>/<chunk_path>``.

    This is the live Epic cache-key URI builder used by the agent's Epic
    validator (``agent/routers/epic.py``): it combines the per-manifest
    ``cdn_base`` with each chunk's ``chunk_path`` to produce the ``uri`` fed
    into ``cache_key(identifier, uri, slice_range)``.
    """
    return f"{cdn_base_path.rstrip('/')}/{chunk_path}"


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
    widths = parse_levels(levels)
    parts: list[str] = []
    end = len(h)
    for w in widths:
        parts.append(h[end - w : end])
        end -= w
    result = cache_root.joinpath(*parts, h)
    # D8 path-containment backstop: the segments are slices of validated
    # hex so escape is structurally impossible, but assert it anyway to
    # catch any future refactor that lets a non-hex segment through.
    if not result.is_relative_to(cache_root):
        raise ValueError(f"computed cache path {result} escapes root {cache_root}")
    return result


def parse_levels(levels: str) -> list[int]:
    """Parse an nginx ``levels`` string into width ints, validating bounds.

    Each width must be >= 1 and the total must not exceed 32 (the md5 hex
    length) — otherwise the directory slicing would silently wrap/produce
    garbage paths (bug A). Raises ``ValueError`` on any malformed value.
    """
    if not levels:
        raise ValueError("cache_levels must be non-empty (e.g. '2:2')")
    try:
        widths = [int(x) for x in levels.split(":")]
    except ValueError as e:
        raise ValueError(f"cache_levels has a non-integer width: {levels!r}") from e
    if any(w < 1 for w in widths):
        raise ValueError(f"cache_levels widths must each be >= 1: {levels!r}")
    if sum(widths) > 32:
        raise ValueError(
            f"cache_levels widths sum to {sum(widths)} > 32 (md5 hex length): {levels!r}"
        )
    return widths
