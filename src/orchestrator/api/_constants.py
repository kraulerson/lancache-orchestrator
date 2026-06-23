"""Pure middleware/auth constants (no DB import).

Living here (rather than in api.dependencies, which pulls the DB pool) lets the
data-plane agent load the middlewares without dragging in the control-plane DB
pool (ARCH-4)."""

from __future__ import annotations

import re

# Body cap (32 KiB per Bible §9.2)
BODY_SIZE_CAP_BYTES: int = 32 * 1024

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
    # F10 status page: serves a single HTML file at GET /. The JS in
    # the page prompts for the bearer + uses it for subsequent API
    # calls (Bible §9.3). The page fetch itself is unauthenticated;
    # all data fetches inside it ARE auth-gated by the existing
    # middleware on /api/v1/* paths.
    ("/", False),
)

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
    # BL10 F1: `/auth/{challenge_id}` (2FA submit) is loopback-only.
    # `/auth/status` is NOT (Game_shelf reads it) — exempted via
    # negative-lookahead.
    re.compile(r"^/api/v1/platforms/[^/]+/auth/(?!status$)[^/]+$"),
    re.compile(r"^/api/v1/openapi\.json$"),
    re.compile(r"^/api/v1/docs/?$"),
    re.compile(r"^/api/v1/docs/oauth2-redirect/?$"),
    re.compile(r"^/api/v1/redoc/?$"),
)

# Loopback host values accepted by the OQ2 check. Both IPv4 (127.0.0.1) and
# IPv6 (::1, ::ffff:127.0.0.1) forms must be honored — UAT-3 S3-h fix.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
