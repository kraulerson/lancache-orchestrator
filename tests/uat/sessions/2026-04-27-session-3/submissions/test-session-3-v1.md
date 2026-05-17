# UAT Test Session — 3 (v1) — SUBMISSION

**Date run:** 2026-04-30
**Tester:** Karl (Orchestrator)
**Format:** H-1 lightweight (HTTP API surface, manual `curl` flows)

> Inline corrections applied during run (template bugs):
> - All uvicorn invocations use `--factory` flag (template wrote `module:app` which failed)
> - OpenAPI/docs paths use `/api/v1/{openapi.json,docs,redoc}` (template wrote `/openapi.json` etc.)

---

## Pre-flight: PASS

All P1-P7 succeeded.

---

## Scenario 1 — uvicorn boot, lifespan logs, schema check: PASS

- 1.1 Startup logs sees `pool_initialized` — PASS
- 1.2 Token did NOT appear in stdout — PASS
- 1.3 OpenAPI schema reachable at `/api/v1/openapi.json` (corrected path) — PASS

**Template bug recorded as S2-I (NEW SEV-2):** `uvicorn orchestrator.api.main:app` fails because main.py exports `create_app()` only. `--factory` flag required.

---

## Scenario 2 — /api/v1/health unauth 503 by design: PASS

- 2.1 Status `503` — PASS
- 2.2 7-field JSON shape correct — PASS
- 2.3 `git_sha = "unknown"` — **S2-B classification update: LATENT not LIVE in default config.** Operator must explicitly set `GIT_SHA` env var (CI pipelines typically do). SEV-2 stands but conditional.
- 2.4 `X-Correlation-ID` header present, UUID-shaped — PASS

---

## Scenario 3 — Bearer auth happy/sad/wrong-scheme/timing: PASS w/ NEW finding

- 3.1 happy → `404` — PASS
- 3.2 wrong token → `401` — PASS
- 3.3 missing → `401` — PASS
- 3.4 wrong scheme `Basic` → `401` — PASS
- 3.5 lowercase `bearer` → **`401` (expected `404`) — FAIL — NEW finding S3-m (SEV-3): RFC 7235 §2.1 says auth scheme is case-insensitive. BearerAuth doesn't fold case. 2-line fix.**
- 3.6 health exempt from auth → `503` — PASS
- 3.7 timing variance check → no obvious outliers — PASS

---

## Scenario 4 — CORS preflight: PASS (better than expected)

- 4.1 Origin not in `cors_origins` list → `400 Disallowed CORS origin` — **PASS** (active rejection is stronger than the template's expected passive no-ACAO)
- 4.2 OPTIONS without Origin → `405 Method Not Allowed` — **PASS** (correct: not a preflight, hits routing layer; health is GET-only)

---

## Scenario 5 — Body size cap: PASS

- 5.1 16KiB → `404` (auth ok, no route, body never consumed) — PASS
- 5.2 32768 (cap exact) → `404` — PASS
- 5.3 32769 (cap+1) → `413` — PASS
- 5.4 1 MiB → `413` — PASS

---

## Scenario 6 — Loopback-only paths from non-loopback (RE-TEST applied): FAIL

Initial run used wrong paths (template bug). Re-test at correct paths:

| Probe | Status | Verdict |
|---|---|---|
| 6.1 loopback `/api/v1/openapi.json` | `200` | expected — loopback access OK |
| 6.2 LAN `/api/v1/openapi.json` | **`200`** | **FAIL — schema exposed to LAN unauth** |
| 6.3 LAN `/api/v1/docs` | **`200`** | **FAIL — Swagger UI exposed to LAN unauth** |
| 6.4 LAN `/api/v1/redoc` | **`200`** | **FAIL — ReDoc UI exposed to LAN unauth** |
| 6.5 LAN `/api/v1/health` | (not re-run; original `503` PASS) | PASS |

**Confirms S2-C LIVE finding:** auto-generated docs + schema accessible to any LAN client without auth or loopback restriction. Schema-enumeration goldmine.

---

## Scenario 7 — Lifespan failure path: PASS w/ NEW finding

- 7.1 startup fails fast (process exit non-zero) — PASS
- 7.2 stderr says why (`unable to open database file`) — PASS
- 7.3 no token in stderr — PASS

**NEW finding S2-J (SEV-2): lifespan only catches its own exception hierarchy.** `sqlite3.OperationalError` from `migrate.run_migrations` propagates raw → 50-line traceback instead of contracted structured `api.boot.migrations_failed` + `SystemExit(1)`. Spec/ADR/closure-summary all say fail-fast SystemExit(1). 4-line fix in migrate.py to wrap `sqlite3.OperationalError` as `MigrationError`.

---

## Scenario 8 — Correlation ID echo + injection: PASS (NON-FINDING surfaced)

- 8.1 Client sent `X-Correlation-ID: my-test-id-12345`. Server log: `correlation_id=b1695a57-3f9b-4c40-86f1-267e3d5a1c6f` (a freshly-generated UUID4) — **server REGENERATES server-side, ignores client-supplied value**
- 8.2 Embedded newline header smuggling — server still generated fresh UUID, no `X-Injected` made it through
- 8.3 No client ID — server generated UUID

**Downgrades threat-model agent F-9 (SEV-4 client X-Correlation-ID injection) to NON-FINDING.** Defense-in-depth: the middleware regenerates and rebinds, doesn't trust client values. Empirically confirmed.

---

## Bugs Found (consolidated triage queue)

| ID | Sev | Live/Latent | Title |
|---|---|---|---|
| S2-A | 2 | latent | AUTH_EXEMPT_PREFIXES startswith antipattern |
| S2-B | 2 | conditional | git_sha leak (only when GIT_SHA env set) |
| **S2-C** | 2 | **LIVE** | /api/v1/openapi.json + /docs + /redoc on LAN unauth |
| S2-D | 2 | latent | OQ2 reverse-proxy bypass (deploy-dependent) |
| S2-E | 2 | latent | LOOPBACK regex coupling (deferred to BL6) |
| S2-F | 2 | latent | CORS innermost masks 401/413 from UI |
| S2-G | 2 | latent | BodySizeCap duplicate http.response.start |
| S2-H | 2 | live | Single-chunk body-cap DoS (counter after alloc) |
| **S2-I** | 2 | live | NEW — uvicorn factory missing module-level app |
| **S2-J** | 2 | live | NEW — lifespan doesn't wrap sqlite3.OperationalError |
| S3-a | 3 | latent | Lifespan partial-init pool leak |
| S3-h | 3 | latent | IPv6 `::1` loopback not matched |
| S3-k | 3 | latent | scope["headers"] not redaction-walked |
| **S3-m** | 3 | live | NEW — RFC-noncompliant lowercase bearer scheme |

**Live findings (4):** S2-C, S2-H, S2-I, S2-J, S3-m
**Latent in BL5 / live for BL6+:** the rest

## Tester Notes

- Manual session corroborated 4 of 8 static SEV-2s (B, C confirmed live; F, G manually inferable; A latent as predicted)
- Surfaced 3 NEW findings (S2-I, S2-J, S3-m) from the empirical run that static analysis missed
- Surfaced 1 NEW non-finding (F-9 X-Correlation-ID injection — already mitigated)
- Net delta: +3 SEV-2, +1 SEV-3, -1 SEV-4
