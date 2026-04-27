"""Shared dependencies, constants, and the version string for the API layer.

Per spec §4: AUTH_EXEMPT_PREFIXES + LOOPBACK_ONLY_PATTERNS (path constants
read by BearerAuthMiddleware) + get_pool_dep (FastAPI dependency wrapping
the BL4 pool singleton).
"""

from __future__ import annotations

import re

from orchestrator.db.pool import Pool, get_pool

# Body cap (32 KiB per Bible §9.2)
BODY_SIZE_CAP_BYTES: int = 32 * 1024

# API version surfaced in /health
__version__: str = "0.1.0"

# Path prefixes that bypass BearerAuthMiddleware (spec §3.3 + §4)
AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/v1/health",
    "/api/v1/openapi.json",
    "/api/v1/docs",
    "/api/v1/redoc",
)

# Path patterns that ADDITIONALLY require client.host == "127.0.0.1" (OQ2)
LOOPBACK_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/v1/platforms/[^/]+/auth$"),
)


async def get_pool_dep() -> Pool:
    """FastAPI dependency wrapping orchestrator.db.pool.get_pool().

    Raises PoolNotInitializedError if init_pool() was not called during
    lifespan startup. Tests override this via app.dependency_overrides.
    """
    return get_pool()
