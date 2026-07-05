"""Agent path-safety guard for destructive cache operations (F18 purge).

A single choke point every unlink must pass through: only paths whose *resolved*
location is strictly inside the lancache cache root survive. This bounds the blast
radius of a bug in the manifest->cache-path enumeration (or a crafted manifest) to
"delete a chunk inside the cache" -- never a file elsewhere on the host, and never
the cache root directory itself. See ADR-0015.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_log = structlog.get_logger(__name__)


def under_cache_root(cache_root: Path, paths: list[Path]) -> list[Path]:
    """Return only the paths that resolve to a location strictly inside ``cache_root``.

    ``Path.resolve()`` collapses ``..`` segments and follows symlinks, so a
    traversal attempt (``cache_root/../../etc/passwd``) or a symlink pointing
    outside the tree resolves outside ``cache_root`` and is dropped + logged.
    The root itself is rejected (it is not one of its own parents), so a purge
    can never target the cache directory.
    """
    root = cache_root.resolve()
    safe: list[Path] = []
    for p in paths:
        resolved = p.resolve()
        if root in resolved.parents:
            safe.append(p)
        else:
            _log.warning("purge.path_outside_cache_root", path=str(p), resolved=str(resolved))
    return safe
