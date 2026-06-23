"""Shared dependencies, constants, and the version string for the API layer.

Per spec §4: AUTH_EXEMPT_PREFIXES + LOOPBACK_ONLY_PATTERNS (path constants
read by BearerAuthMiddleware) + get_pool_dep (FastAPI dependency wrapping
the BL4 pool singleton).
"""

from __future__ import annotations

from orchestrator.api._constants import (
    AUTH_EXEMPT_PATHS,
    BODY_SIZE_CAP_BYTES,
    LOOPBACK_HOSTS,
    LOOPBACK_ONLY_PATTERNS,
)
from orchestrator.db.pool import Pool, get_pool

# Re-export the pure middleware/auth constants from orchestrator.api._constants
# (moved there in ARCH-4 so middleware.py can import them without dragging in the
# DB pool). Kept in __all__ so existing `dependencies.<CONST>` importers still work.
__all__ = [
    "AUTH_EXEMPT_PATHS",
    "AUTH_EXEMPT_PREFIXES",
    "BODY_SIZE_CAP_BYTES",
    "LOOPBACK_HOSTS",
    "LOOPBACK_ONLY_PATTERNS",
    "get_pool_dep",
]

# API version surfaced in /health
__version__: str = "0.1.0"

# Backwards-compatibility view for older imports (just the path strings).
AUTH_EXEMPT_PREFIXES: tuple[str, ...] = tuple(p for p, _ in AUTH_EXEMPT_PATHS)


async def get_pool_dep() -> Pool:
    """FastAPI dependency wrapping orchestrator.db.pool.get_pool().

    Raises PoolNotInitializedError if init_pool() was not called during
    lifespan startup. Tests override this via app.dependency_overrides.
    """
    return get_pool()
