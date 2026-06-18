# Orchestrator LAN-bind + Source-IP Allowlist — Design

**Date:** 2026-06-18
**Status:** Approved (design)
**Branch:** `feat/lan-bind-allowlist`
**Driver:** Game_shelf (F14–F17, merged) consumes the orchestrator API from a different host (LXC1102 @ `10.100.23.102`). The orchestrator runs dockerized on the lancache host (`192.168.1.40`) and is currently loopback-only. It must be reachable from Game_shelf without weakening any existing protection.

---

## 1. Goal

Allow the orchestrator API to be bound to the LAN so Game_shelf can reach it, while guaranteeing — at the application layer, fail-closed — that only declared source IPs can connect. Security is priority #1: the change must not weaken the bearer-token gate or the OQ2 loopback gate, and it must be impossible to LAN-expose the API by accident.

## 2. Decisions (locked during brainstorming)

- **Transport: plaintext HTTP + source allowlist.** The VLAN between the two Proxmox guests is treated as a trusted segment. The bearer token plus a fail-closed source-IP allowlist are the gates; no TLS, no reverse proxy. (A reverse proxy would also silently defeat OQ2/allowlist, which read `scope["client"]` directly.)
- **Firewall: in-repo app allowlist + documented host firewall.** The application-level allowlist is the in-repo, tested deliverable. The host nftables/iptables rule is documented in the deploy recipe for the operator to apply with sudo (host sudo is password-gated; it stays a manual step).

## 3. What already exists (no change needed)

- **Bind is env-driven.** The Dockerfile entrypoint runs `python -m uvicorn … --host "${ORCH_API_HOST:-127.0.0.1}" --port "${ORCH_API_PORT:-8765}"`. Setting `ORCH_API_HOST` is all that's needed to bind to the LAN. No code change for the bind itself.
- **OQ2 loopback gate** (`api/middleware.py` `BearerAuthMiddleware`, `LOOPBACK_ONLY_PATTERNS` in `api/dependencies.py`) restricts credential-intake (`POST /platforms/{name}/auth`), 2FA-submit (`/platforms/{name}/auth/{challenge_id}`, except `/auth/status`), and schema endpoints (`openapi.json`, `docs`, `redoc`) to loopback by reading `scope["client"][0]`. A LAN bind therefore keeps these unreachable from Game_shelf automatically. Game_shelf only ever reaches the bearer-gated data/action endpoints + `/auth/status` it proxies in F14.
- **Bearer auth** (`BearerAuthMiddleware`) gates every non-exempt `/api/v1/*` path with a constant-time token compare.
- **Non-loopback bind warning** (`api/main.py` lifespan, via `_detect_non_loopback_bind()`) already detects a non-loopback bind from settings / `UVICORN_HOST` / `--host` argv. This design upgrades that warning into a fail-closed guard.

## 4. The deliverable: fail-closed source-IP allowlist

A thin defense-in-depth layer **outside** the bearer token — the "trust-list of proxy IPs" the code flags as Phase-3 backlog (`api/dependencies.py:58`). It restricts which source IPs may open a connection to the API at all, on **every** path (including the unauthenticated `/health` and the schema endpoints), before any auth or body processing.

### 4.1 New setting (`core/settings.py`)

```
allowed_source_ips: list[str] = []   # env ORCH_ALLOWED_SOURCE_IPS
```

- **Env parsing:** accept either a comma-separated string (`10.100.23.102,10.0.0.0/24`) or a JSON array, via a `field_validator(mode="before")` that splits on comma + strips whitespace + drops empties. (Mirrors how `cors_origins` is fed; the planning step confirms and reuses the existing mechanism.)
- **Validation:** a `field_validator(mode="after")` parses each entry with `ipaddress.ip_network(entry, strict=False)`. An invalid entry raises a `ValidationError` at boot (fail fast). Parsed networks are exposed via a cached property `allowed_source_networks -> list[IPv4Network | IPv6Network]` so the middleware doesn't re-parse per request.
- **Default `[]`** = no extra sources beyond loopback. For the default loopback-only deployment this is a no-op.
- **Allow-any is explicit and auditable:** `ORCH_ALLOWED_SOURCE_IPS=0.0.0.0/0` (and/or `::/0`) opts into "any source," a deliberate choice visible in config and logged at boot.

### 4.2 Always-allowed loopback

Loopback (`127.0.0.1`, `::1`, `::ffff:127.0.0.1`) is **always** allowed, independent of the configured list — the CLI, the docker healthcheck, the F10 status page, and the OQ2 loopback endpoints all originate from loopback. The allowlist is **additive** to loopback. Reuse `LOOPBACK_HOSTS` from `api/dependencies.py`.

### 4.3 New `SourceAllowlistMiddleware` (`api/middleware.py`)

A dedicated single-responsibility ASGI middleware (not folded into `BearerAuthMiddleware`), with a pure, independently-testable matching helper:

```
def _is_source_allowed(client_host: str | None,
                       allowed_networks: list[IPv4Network | IPv6Network]) -> bool:
    # True if client_host is a loopback form, or parses to an IP contained
    # in any allowed network. False on None/unparseable, or no match (fail closed).
```

- **Enforcement switch (critical):** the middleware enforces **only when `allowed_source_networks` is non-empty**. With an empty list it is a pure passthrough (allow-all). This is safe and required:
  - The §4.4 boot guard guarantees an empty allowlist can only coincide with a loopback-only bind, where the only possible clients are loopback anyway.
  - It keeps the default/dev/test posture unchanged: the existing test suite hits endpoints via Starlette `TestClient` whose client host is `"testclient"` (neither loopback nor allowlisted); an always-rejecting middleware would 403 the entire suite. Empty-list-is-no-op avoids that. Enforcement tests set a non-empty allowlist explicitly and control the client host.
- When enforcing, reads `scope["client"][0]` directly. **No `X-Forwarded-For` trust** — uvicorn is bound directly with no reverse proxy, so the peer IP is authentic.
- Loopback forms (`LOOPBACK_HOSTS`) are always allowed when enforcing. Normalizes IPv4-mapped IPv6 (`::ffff:a.b.c.d`) before matching so a mapped client still matches an IPv4 network entry.
- On a disallowed source (only possible while enforcing): log `api.source.rejected` (reason, client_host, path) and return **403** `{"detail":"forbidden: source not allowed"}` before any downstream work. Reuse the existing `_send_403`-style helper shape.
- A `None`/missing client while enforcing is treated as **not allowed** — fail closed.

**Stack placement.** `add_middleware` prepends, so the registration becomes (innermost → outermost):

```
BearerAuthMiddleware           # innermost
BodySizeCapMiddleware
SourceAllowlistMiddleware      # NEW
CorrelationIdMiddleware
CORSMiddleware                 # outermost
```

giving request-time order **CORS → CorrelationId → SourceAllowlist → BodySizeCap → BearerAuth**. Rationale: the source gate sits *inside* CorrelationId (so a rejected source still gets a correlation-id and the `request.received`/`completed` log lines for ops visibility) but *outside* body-size and auth processing (an unknown source is dropped before we read its body or touch the token). CORS remains outermost; a non-allowlisted host's OPTIONS preflight is answered by CORS with no data exposure (and `cors_origins` is empty by default), while its actual GET/POST is 403'd by the source gate.

The middleware is **always registered**; with an empty allowlist and loopback clients it is a no-op.

### 4.4 Fail-closed boot guard (`api/main.py` lifespan)

Move the non-loopback-bind check to the **top** of lifespan (right after `settings = get_settings()`, before migrations) so a misconfiguration fails fast without spinning up the pool/worker/scheduler. Replace today's warning-only behavior:

```
bind_signal = _detect_non_loopback_bind(settings.api_host)
if bind_signal is not None:
    if not settings.allowed_source_ips:
        log.critical("api.boot.lan_bind_without_allowlist",
                     api_host=bind_signal,
                     hint="Set ORCH_ALLOWED_SOURCE_IPS to the permitted source(s) "
                          "before binding off-loopback. Refusing to start.")
        raise SystemExit(1)
    log.info("api.boot.lan_bind_gated",
             api_host=bind_signal,
             allowed_source_ips=settings.allowed_source_ips,
             loopback_only_endpoints="auth/2fa/schema remain loopback-only (OQ2)")
```

`raise SystemExit(1)` aborts startup the same way the existing migration/pool-init failures do. You cannot bind off-loopback without declaring who's allowed.

## 5. What stays safe automatically

- **OQ2 is unchanged and still loopback-only.** An allowlisted Game_shelf is *not* loopback, so it still cannot reach credential-intake, 2FA-submit, or schema endpoints. The allowlist grants connection eligibility, **not** loopback-equivalence.
- **Bearer auth is unchanged.** Allowlisted sources still need a valid token for `/api/v1/*`; `/health` and `/auth/status` remain the only LAN-reachable unauthenticated/low-sensitivity reads, exactly as F14 expects.
- **No new trust of client-supplied headers.** Matching uses the transport peer IP only.

## 6. Deploy recipe (documentation, not code)

Update the deploy recipe / HANDOFF with the LAN-exposure configuration:

- `ORCH_API_HOST=0.0.0.0` — the container binds all interfaces *inside its own network namespace*.
- Docker publish **scoped to the LAN NIC**: `-p 192.168.1.40:8765:8765` — host-side exposure is limited to the LAN IP, not every host interface (e.g. not a separate management NIC).
- `ORCH_ALLOWED_SOURCE_IPS=10.100.23.102` — Game_shelf LXC.
- Outer-layer host firewall (operator applies with sudo), documented as an nftables rule allowing only `10.100.23.102 → tcp/8765` and dropping otherwise. This is belt-and-suspenders over the app allowlist.
- Game_shelf side (already built): set `ORCH_API_URL=http://192.168.1.40:8765` in the Game_shelf backend env (the F14 proxy already injects the bearer server-side).

## 7. Testing (test-first, QA-engineer mindset)

**Pure matching (`_is_source_allowed`)** — exercised with a non-empty `allowed_networks`
- loopback `127.0.0.1` / `::1` / `::ffff:127.0.0.1` allowed regardless of the entries
- exact IP entry (`10.100.23.102`) allows that IP, rejects a neighbor (`10.100.23.103`)
- CIDR entry (`10.0.0.0/24`) allows in-range, rejects out-of-range
- IPv4-mapped IPv6 client (`::ffff:10.100.23.102`) matches an IPv4 entry
- `0.0.0.0/0` allows an arbitrary IPv4 source
- `None`/unparseable client → not allowed (fail closed)

**Middleware integration (ASGI / TestClient)** — note the enforcement switch
- **empty allowlist** + arbitrary source (`"testclient"`) → passthrough, normal behavior (proves the suite-safe no-op)
- non-empty allowlist, non-allowlisted source → **403** on `/api/v1/health` *and* `/api/v1/games` (gated before auth)
- non-empty allowlist, allowlisted source, no token → **401** on `/api/v1/games` (passes source gate, fails bearer)
- non-empty allowlist, allowlisted source, valid token → **200** on `/api/v1/games`
- non-empty allowlist, allowlisted-but-non-loopback source → **403** on `POST /api/v1/platforms/steam/auth` (OQ2 still bites)
- non-empty allowlist, rejected source still receives an `X-Correlation-ID` (CorrelationId is outer)

**Settings validation**
- comma-separated env string parses to a list; surrounding whitespace trimmed; empty entries dropped
- invalid CIDR (`10.0.0.0/99`, `not-an-ip`) → `ValidationError` at construction
- `allowed_source_networks` returns parsed network objects

**Boot guard (lifespan)**
- non-loopback `api_host` + empty allowlist → `SystemExit(1)`, logs `lan_bind_without_allowlist`
- non-loopback `api_host` + non-empty allowlist → boots, logs `lan_bind_gated`
- loopback `api_host` + empty allowlist → boots normally (no guard trip)

## 8. Scope boundary (YAGNI)

Out of scope: TLS / HTTPS, reverse proxy, `X-Forwarded-For` parsing, dynamic/hot allowlist reload (env-at-boot only), a scripted host firewall (documented only), per-endpoint allowlists (single global list). These are explicitly excluded; revisit only if the threat model changes (e.g. an untrusted LAN segment → TLS).

## 9. Files touched

- `src/orchestrator/core/settings.py` — new `allowed_source_ips` field + validators + `allowed_source_networks` cached property.
- `src/orchestrator/api/middleware.py` — new `_is_source_allowed` helper + `SourceAllowlistMiddleware`.
- `src/orchestrator/api/main.py` — register the middleware in the stack; move + harden the bind guard into a fail-closed check at the top of lifespan.
- `src/orchestrator/api/dependencies.py` — (reuse `LOOPBACK_HOSTS`; export if needed).
- Tests: `tests/api/test_source_allowlist.py` (new), additions to settings + lifespan/boot tests.
- Docs: deploy recipe / HANDOFF LAN-exposure section; `CHANGELOG.md` (Security + Added + Infrastructure), `FEATURES.md`.
