# UAT-3 Consolidated Findings & Triage Matrix

**Date:** 2026-04-27
**Branch:** `feat/uat-3-session`
**Scope:** BL5 — FastAPI skeleton (`src/orchestrator/api/{main,dependencies,middleware,routers/health}.py`)
**Agents:** sast-middleware, threat-model, input-validation, auth-lifespan, logging-redaction

## Tooling sweep (all clean)

| Tool | Result |
|---|---|
| ruff | 0 |
| mypy --strict | 0 |
| semgrep p/owasp-top-ten | 0/152 |
| semgrep project rules | 0/7 |
| gitleaks (97 commits) | 0 |

## Severity tally (post-manual-session, 2026-04-30)

- **SEV-1:** 0
- **SEV-2:** **11** unique (8 from agents + 3 new from manual session: S2-I, S2-J + S2-C upgraded LATENT→LIVE)
- **SEV-3:** **12** (11 from agents + 1 new from manual session: S3-m)
- **SEV-4:** 12 (13 from agents − 1 mitigated: F-9 X-Correlation-ID injection empirically confirmed safe)

## Manual session deltas (2026-04-30)

Net: +3 SEV-2, +1 SEV-3, −1 SEV-4. Agent classification accuracy: 7/8 SEV-2s confirmed; F-9 SEV-4 wrongly assumed accept-and-use (server actually regenerates); 3 new findings agents missed.

| ID | Source | Note |
|---|---|---|
| S2-C | agent (latent) → manual (LIVE) | LAN openapi/docs/redoc all return `200` unauth — confirmed |
| S2-I | manual only | `uvicorn module:app` fails — factory pattern, no module-level app |
| S2-J | manual only | `sqlite3.OperationalError` propagates raw past lifespan catch (catches own hierarchy only) |
| S3-m | manual only | RFC 7235 case-insensitive scheme; `bearer` lowercase returns 401 instead of accepting |
| F-9 (mitigated) | agent → manual | Server regenerates correlation_id; client value ignored. NON-FINDING |

## SEV-2 unique findings (deduplicated across agents)

| # | Title | Live or latent? | Found by | Fix sketch |
|---|---|---|---|---|
| **S2-A** | `AUTH_EXEMPT_PREFIXES` uses unanchored `path.startswith(p)` — `/api/v1/healthxxx`, `/api/v1/docszzz` etc. silently bypass auth in any future BL | latent (no offending route exists today) | sast (SEV-3 cluster), threat-model F-8, input-validation F9, auth-lifespan SEV-2-A | `path == p or path.startswith(p + "/")` for all 4 prefixes |
| **S2-B** | `/api/v1/health` returns 40-char `git_sha` to unauth callers — pre-token recon on open-source repo (TM-013 deferral material) | LIVE today | threat-model F-1 | Gate `git_sha` behind auth, OR truncate to 8 chars, OR remove from unauth path |
| **S2-C** | `/openapi.json`, `/docs`, `/redoc` are auth-exempt AND non-loopback — full API map exposed to anyone reaching port 8765 | LIVE for schema, latent until BL6+ for impact | threat-model F-6 | Either require auth or restrict to loopback (recommend loopback per OQ2 pattern) |
| **S2-D** | OQ2 loopback reads `scope["client"][0]` directly. Same-host reverse proxy (e.g., nginx TLS terminator) silently bypasses OQ2 — every request appears as 127.0.0.1 | latent (deployment-dependent) | threat-model F-7 | Documented deployment constraint + optional `OQ2_TRUSTED_PROXIES` allowlist + warn on startup if non-loopback bind without explicit trust list |
| **S2-E** | `LOOPBACK_ONLY_PATTERNS` is opt-in regex tuple, not coupled to route declaration. A future BL adding a privileged endpoint can forget the regex update; default = non-loopback | latent | auth-lifespan SEV-2-B, input-validation F10 | Couple to route via decorator/marker, OR document + add CI lint, OR invert to deny-by-default |
| **S2-F** | CORS is innermost. 401/413/403 short-circuits lack `Access-Control-Allow-Origin`. Once a UI origin is in `cors_origins`, browser surfaces "CORS error" instead of "401", masking auth/cap failures from operators | latent until cors_origins populated | sast SEV-2 #2 | Move CORSMiddleware to outermost; bearer-auth already short-circuits OPTIONS so preflight still works |
| **S2-G** | `BodySizeCap` can emit duplicate `http.response.start`. When BL6+ ships a streaming handler that interleaves body-read with response-write, mid-response cap rejection produces a second start frame — ASGI protocol violation | latent until streaming handler exists | sast SEV-2 #1 | Track `started: bool` flag in wrapped send; if started, switch to log-and-disconnect on cap |
| **S2-H** | Single-chunk body-cap DoS: streaming counter does `bytes_received += len(body)` AFTER chunk is in memory. A single oversized `http.request` chunk allocates before being rejected | LIVE for BL5 surface (no body endpoints, but the middleware is reachable) | input-validation F13 | Per-chunk size check at receive; reject if `len(body) > cap` immediately |

## SEV-3 findings (selected highlights)

| # | Title | Source |
|---|---|---|
| S3-a | Lifespan partial-init: if `init_pool()` succeeds but `app.state` assignment / subsequent setup fails, no `close_pool()` runs. Pool/connections leaked at process death | auth-lifespan, sast |
| S3-b | TM-013 log-channel fingerprinting: `api.auth.rejected` logs differential `reason=`/`path=`/`fingerprint=` — operator with log access can enumerate. (Mostly inside-attacker; deferred to Phase 3 was acceptable but worth re-checking.) | sast |
| S3-c | "Lying Content-Length" can defer to streaming path; current streaming check is correct, but cap rejection log doesn't fire for the proactive path mismatch case. Verify | sast |
| S3-d | `git_sha` deferred per TM-013 but BL5 made it concrete (linked to S2-B) | sast, threat-model |
| S3-e | WebSocket scope bypasses ALL four middlewares (all middleware is `if scope["type"] != "http": pass through`) — latent until BL7 ships a WS endpoint | threat-model |
| S3-f | No global Exception handler — 500 responses lack X-Correlation-ID echo, hampering operator log correlation | threat-model |
| S3-g | `uvicorn limit_concurrency` not set — slowloris-class DoS resilience | threat-model |
| S3-h | Loopback check uses literal `"127.0.0.1"` string vs hostname normalization — IPv6 `::1` doesn't match. Also `::ffff:127.0.0.1` IPv4-mapped form | auth-lifespan |
| S3-i | No `..` rejection in path normalization layer (currently relies on Starlette behavior; verified safe today, but layer fragility) | auth-lifespan |
| S3-j | 404-vs-401 status differential under exempt-prefix probing acts as namespace-enumeration oracle | auth-lifespan |
| S3-k | `_redact_sensitive_values` matches dict keys only — `scope["headers"]` (list of `(bytes, bytes)` tuples) bypasses redaction if any future code logs `scope=scope` | logging-redaction |

## SEV-4 (compact)

ASCII-decode-truncates-non-ASCII tokens (silent operator footgun); 7 other small hardening items spanning header parsing, redaction allowlist, regression test gaps, OpenAPI bearerAuth metadata, env-var GIT_SHA injection. Full detail in per-agent reports.

## Most surprising findings

1. **`/api/v1/health` returns full 40-char git_sha to unauth callers** — concrete recon leak on a public repo. Phase-3 deferral was reasonable in the abstract but BL5 made it a live exposure.
2. **OQ2 loopback brittle to reverse-proxy topology** — operator running nginx for TLS termination silently disables loopback enforcement entirely.
3. **AUTH_EXEMPT_PREFIXES startswith antipattern** — found by 4 of 5 agents independently. Latent today, but a single future router prefix collision becomes a public unauth endpoint.
4. **Most surprising NON-finding:** path-traversal / URL-encoding / mixed-case attempts against AUTH_EXEMPT and LOOPBACK_ONLY_PATTERNS produced zero exploit (Starlette literal route matching defeats encoding tricks at the regex layer). Brittleness is in deployment topology, not wire protocol.

## Triage decisions needed (Orchestrator)

For each SEV-2 below, choose: **Fix Now / Defer to Phase 3 / Won't Fix / Pre-merge BL6 hardening sprint**.

| ID | Recommendation | Rationale |
|---|---|---|
| S2-A (startswith) | **Fix Now** | 5-line fix; found by 4 agents; prevents future foot-gun; cheapest hardening on the matrix |
| S2-B (git_sha leak) | **Fix Now** | Live recon leak on open-source repo; one-line truncation or auth gate |
| S2-C (openapi/docs/redoc) | **Fix Now** (loopback-restrict) | Aligns with OQ2 model already established; minimal code |
| S2-D (reverse-proxy OQ2) | **Document + warn** (this UAT) + **Defer enhanced trust list to Phase 3** | Fix is config-shape change; documentation is sufficient short-term |
| S2-E (LOOPBACK regex coupling) | **Defer to BL6 hardening sprint** (couple decision to platform router) | Best fixed when first non-trivial loopback-only route lands |
| S2-F (CORS innermost) | **Fix Now** | One-line ordering swap; eliminates UI debugging trap |
| S2-G (duplicate response.start) | **Fix Now** (small) | Cheap defensive guard; future-proofs streaming handlers |
| S2-H (single-chunk body DoS) | **Fix Now** | Live in BL5; per-chunk pre-check is ~3 lines |

**Per memory `feedback_default_to_most_capable.md`**: recommend **fixing all 8 SEV-2 now** (most-capable option) rather than deferring. Effort is hours, not days; surface area is contained; regression tests sketched per finding.

SEV-3 cluster recommendation: fix S3-a (lifespan partial-init), S3-h (IPv6 loopback), S3-k (scope.headers redaction). Defer rest to issue tracker.

## Next steps in UAT-3 checklist

1. Mark `agents_dispatched` complete (after this consolidation written)
2. Generate H-1 lightweight manual test session template
3. Surface findings + manual session to operator
4. Wait on triage decisions before remediation
5. Test-first remediation per Fix-Now items
6. Mark remaining checklist steps; close gate; PR

## File index

- `sast-middleware.md` (27 KB) — tooling + middleware ordering + lifespan paths
- `threat-model.md` (58 KB) — TM-001..TM-023 walk + 8 BL5-specific scenarios
- `input-validation.md` (34 KB) — 8 input-vector fuzz matrices
- `auth-lifespan.md` (31 KB) — auth state matrix + lifespan failure paths
- `logging-redaction.md` (22 KB) — log-call inventory + empirical redaction trace
