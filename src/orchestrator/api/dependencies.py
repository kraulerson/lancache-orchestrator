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

# Paths that bypass BearerAuthMiddleware (spec §3.3 + §4).
#
# UAT-3 S2-A: matching is EXACT-or-subpath. /api/v1/healthxxx must NOT match
# /api/v1/health by accident. /api/v1/docs DOES allow subpaths because
# Swagger UI loads /api/v1/docs/oauth2-redirect; the rest are exact-only.
#
# Format: tuple of (path, allow_subpaths) pairs. Match logic in middleware:
#   exempt = path == p or (allow_subpaths and path.startswith(p + "/"))
AUTH_EXEMPT_PATHS: tuple[tuple[str, bool], ...] = (
    ("/api/v1/health", False),
    ("/api/v1/openapi.json", False),
    ("/api/v1/docs", True),
    ("/api/v1/redoc", False),
)

# Backwards-compatibility view for older imports (just the path strings).
AUTH_EXEMPT_PREFIXES: tuple[str, ...] = tuple(p for p, _ in AUTH_EXEMPT_PATHS)

# Path patterns that ADDITIONALLY require client.host to be loopback (OQ2).
#
# Loopback-only restriction applies to:
#   - POST /api/v1/platforms/{name}/auth (operator credential intake)
#   - /api/v1/openapi.json + /api/v1/docs + /api/v1/redoc (schema enumeration
#     defense — UAT-3 S2-C: schema must not be reachable from LAN even
#     unauthenticated, to prevent route mapping by adjacent attackers).
#
# DEPLOYMENT WARNING (UAT-3 S2-D): the loopback check reads scope["client"][0]
# directly. If a reverse proxy (nginx, traefik, caddy) terminates TLS in
# front of this app, every request will appear to come from 127.0.0.1 and
# OQ2 is silently disabled. Operators running behind a reverse proxy MUST
# either (a) bind the orchestrator to a unix socket only the reverse proxy
# can reach, or (b) accept that loopback enforcement is moot and enforce
# the equivalent at the proxy layer. A trust-list of proxy IPs is on the
# Phase 3 hardening backlog; until then, document and surface the warning
# at boot via a non-loopback bind log line (see api/main.py lifespan).
LOOPBACK_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/v1/platforms/[^/]+/auth$"),
    re.compile(r"^/api/v1/openapi\.json$"),
    re.compile(r"^/api/v1/docs/?$"),
    re.compile(r"^/api/v1/docs/oauth2-redirect/?$"),
    re.compile(r"^/api/v1/redoc/?$"),
)

# Loopback host values accepted by the OQ2 check. Both IPv4 (127.0.0.1) and
# IPv6 (::1, ::ffff:127.0.0.1) forms must be honored — UAT-3 S3-h fix.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


async def get_pool_dep() -> Pool:
    """FastAPI dependency wrapping orchestrator.db.pool.get_pool().

    Raises PoolNotInitializedError if init_pool() was not called during
    lifespan startup. Tests override this via app.dependency_overrides.
    """
    return get_pool()
