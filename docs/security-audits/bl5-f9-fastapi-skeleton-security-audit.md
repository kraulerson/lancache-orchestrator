# Security Audit — BL5 FastAPI Skeleton

**Feature:** BL5-F9-fastapi-skeleton (Build Loop 5, Milestone B)
**Module:** `src/orchestrator/api/` (5 source files: `main.py`, `dependencies.py`, `middleware.py`, `routers/__init__.py`, `routers/health.py`)
**Audit date:** 2026-04-27
**Auditor:** self-review (Senior Security Engineer persona) + automated SAST (semgrep OWASP top-10 + project custom rules) + gitleaks
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-04-27 -->

## Scope

Post-implementation security review of the new BL5 FastAPI skeleton, covering:

- `src/orchestrator/api/main.py` (~140 LoC) — `create_app()` factory, `_lifespan()` async context manager (migrations + pool init + boot metadata + close_pool), 4-layer middleware registration (CORSMiddleware, BearerAuthMiddleware, BodySizeCapMiddleware, CorrelationIdMiddleware), OpenAPI security scheme registration, router mounting
- `src/orchestrator/api/dependencies.py` (~40 LoC) — `BODY_SIZE_CAP_BYTES`, `__version__`, `AUTH_EXEMPT_PREFIXES`, `LOOPBACK_ONLY_PATTERNS`, `get_pool_dep`
- `src/orchestrator/api/middleware.py` (~250 LoC) — three pure-ASGI middlewares: `CorrelationIdMiddleware` (UUID4 validation + `request_context`), `BodySizeCapMiddleware` (Content-Length proactive check + streaming `receive()` interception with `_BodyTooLargeError`), `BearerAuthMiddleware` (`hmac.compare_digest` + exempt prefixes + OPTIONS bypass + OQ2 loopback enforcement + `rejection_fingerprint` logging)
- `src/orchestrator/api/routers/health.py` (~75 LoC) — `HealthResponse` Pydantic model with `extra="forbid"`, `get_health` handler with fail-fast 503 logic
- 7 test files in `tests/api/` (~700 LoC) — 48 tests covering app factory, lifespan, all 3 middlewares, /health endpoint

## Methodology

1. **Automated SAST:** `semgrep scan --config=p/owasp-top-ten --config=.semgrep/` on `src/orchestrator/api/` — 0 findings.
2. **gitleaks** on the staged set — 0 findings.
3. **ruff check + format** on `src/orchestrator/api/` and `tests/api/` — clean.
4. **mypy --strict** on full `src/` — clean (20 source files checked).
5. **Threat-model cross-check** against `docs/phase-1/threat-model.md`: TM-001 (token leak), TM-005 (SQL injection — N/A in API layer), TM-012 (log credential leak), TM-013 (fingerprinting), TM-018 (memory bomb), TM-023 (kill chain — OQ2 loopback enforcement on platforms/auth).
6. **Hand audit of every emit-path** in `middleware.py` for raw-token leakage; every header parsing site for malformed-header crash potential.
7. **Test coverage** on `src/orchestrator/api/` — 96% branch (exceeds 95% target).

## Audit findings

| # | Severity | Title | Status |
|---|---|---|---|
| F1 | SEV-3 | **`token_sha256_prefix` field name conflicted with ID3's auto-redaction.** ID3's `_redact_sensitive_values` regex matches keys containing "token", "secret", "auth", "bearer", etc. — replacing the value with `<redacted>`. The intentional 8-hex sha256 prefix (defensive logging for `bad_token` rejection) was being clobbered by the redactor before reaching the JSON serializer. Discovered during green-phase test `test_auth_rejected_event_emits_with_sha256_prefix`. | **FIXED** — Field renamed to `rejection_fingerprint` (no overlap with ID3's sensitive-key regex). The semantic intent is identical (an 8-hex non-reversible defensive identifier); only the field name changed. Regression test pinned. |
| F2 | SEV-4 (information) | **Streaming body-cap is best-effort for unauthenticated requests.** Spec §5.1 ordering puts body-cap OUTSIDE bearer-auth, but the streaming path (no Content-Length header, chunked transfer-encoding) only fires when the downstream app actually calls `receive()`. Bearer-auth rejects before reading the body, so unauthenticated streaming requests don't trip the streaming cap. The Content-Length proactive path still works for unauthenticated requests. | **ACCEPTED** — The threat (TM-018 manifest memory bomb) is moot for unauthenticated requests because no body-buffering occurs (auth rejects before read). For authenticated requests that DO read bodies (BL6+ endpoints), the streaming cap fires correctly — verified by direct middleware unit test (`test_chunked_oversize_rejected_413_via_direct_middleware`). Documented inline in `tests/api/test_middleware_body_size_cap.py` module docstring. |

## Non-findings (explicitly checked, clean)

- **TM-001 bearer-token leak in error responses.** `_send_401` returns a fixed JSON body (`{"detail":"unauthorized"}`); the rejected token is never echoed. WWW-Authenticate header is `Bearer realm="orchestrator"` — no token. Verified by `test_no_raw_token_in_logs` (TestBearerAuthLogging).
- **TM-001 bearer-token leak in logs.** `bad_token` rejection logs `rejection_fingerprint` (8 hex of SHA-256, non-reversible). The token bytes only enter `hashlib.sha256().hexdigest()[:8]` then are dropped. `_log.warning(...)` arguments never include the raw `token` string.
- **TM-012 log credential leak — broader.** `Authorization` header is read into `auth_header` local but never logged directly. The local goes out of scope after the middleware returns. `request_context()` propagates only `correlation_id` (UUID4) — not the auth state.
- **TM-013 fingerprinting via 404.** Bearer-auth runs as ASGI middleware (NOT a FastAPI dependency), so non-exempt 404 paths return 401 (auth fails first), not 404. An attacker probing for endpoints can't distinguish "endpoint exists but I'm unauth'd" from "endpoint doesn't exist." Verified by `test_correct_token_passes_auth` returning 404 (auth passed, route absent) and unauthenticated probes returning 401 across all non-exempt paths.
- **TM-013 fingerprinting via OpenAPI.** `/api/v1/openapi.json` is auth-exempt (per spec §3.3 rationale: schema doesn't leak secrets, schema describes paths). Schema includes server `title` and `version` strings — both are public Docker image tag info, not new fingerprintable data. No request-counter or runtime-state leak.
- **TM-018 memory bomb — Content-Length path.** `BodySizeCapMiddleware._send_413` fires BEFORE any downstream middleware reads the body. Verified by `test_oversize_content_length_rejected_413`. ASGI receive() is never called for over-cap declared sizes.
- **TM-018 memory bomb — streaming path (authenticated).** Direct middleware unit test verifies `receive_with_cap` interrupts the request mid-stream when accumulated body bytes exceed cap. The downstream app is never reached.
- **TM-023 kill chain — OQ2 loopback enforcement.** `LOOPBACK_ONLY_PATTERNS` regex matches `/api/v1/platforms/{name}/auth`. After auth passes, the middleware checks `scope["client"][0] == "127.0.0.1"`. External-IP requests (verified via `httpx.ASGITransport(client=("192.168.1.100", ...))`) get 403. Loopback requests pass to the (BL5-non-existent) handler.
- **`hmac.compare_digest` constant-time on bytes.** Both inputs encoded via UTF-8 before comparison. Length mismatch is handled constant-timely by `hmac.compare_digest` itself (Python stdlib guarantees this on bytes). No leading-prefix or length-leak attack surface.
- **OPTIONS preflight bypass.** Method-based bypass before path checks; CORS middleware at innermost layer handles the actual preflight response. Browsers receive the expected `Access-Control-Allow-*` headers without traversing auth.
- **CSRF.** `allow_credentials=False` on CORS — no cookie auth surface to forge. Bearer auth flows in `Authorization` header (browser doesn't auto-attach to cross-origin requests). CSRF non-applicable.
- **JSON injection / response splitting.** All response bodies are JSON-serialized via `JSONResponse` or hardcoded byte literals. No user input reaches response headers (CID is UUID4, validated; response body for 401/403/413 is fixed).
- **Path traversal in exempt-path matching.** `path.startswith(p)` for AUTH_EXEMPT_PREFIXES. Path comes from ASGI `scope["path"]` (already URL-decoded by uvicorn). The exempt prefixes are explicit strings (`/api/v1/health` etc.); no prefix can be smuggled past via `..` because `startswith` is a literal-prefix check.
- **Lifespan SystemExit propagation.** Migration failure or pool init failure raises `SystemExit(1)` — uvicorn handles cleanly, container's `restart: unless-stopped` triggers retry loop. Bible §5.4 boot-order pattern. Verified via direct `app.router.lifespan_context` invocation in `test_lifespan_migration_failure_raises_systemexit`.
- **`@asynccontextmanager` cleanup safety.** Pool close in `_lifespan` finally block; `close_pool()` carries the BL4 30s hard timeout. PoolError during shutdown is caught and logged at ERROR (doesn't prevent uvicorn shutdown).

## Coverage gap accepted

Branch coverage on `src/orchestrator/api/` is **96%** (236 stmts / 34 branches; 2 partial branches; 8 lines miss). Plan target was ≥95%. Missing branches:

| File:lines | What's not exercised | Why accepted |
|---|---|---|
| `main.py` 56-58 | Lifespan catch for `SchemaNotMigratedError` / `SchemaUnknownMigrationError` / `PoolError` | The migration-failure path triggers `MigrationError` first (V-3 /dev/null reject); the other three exception types would require a more elaborate fault-injection setup (mocking init_pool to raise specific subclasses). Not security-critical — same SystemExit code path. |
| `main.py` 70-71 | Shutdown `PoolError` catch | `close_pool()` rarely fails in tests because the pool was just opened. Would require fault-injection to reach. Same log-and-continue behavior — not security-critical. |
| `main.py` 129 | `custom_openapi` cache hit path | Triggered only on second `app.openapi()` call within the same test. The factory-test exercises the first call. |
| `middleware.py` 123-124 | Content-Length `int()` ValueError fallback | Would require a non-numeric `Content-Length` header. ASGI servers normalize this; in practice the path is unreachable. Defensive code only. |

None of these gaps represent untested security-critical code — the primary middleware decision paths, scrubbing, lifespan happy/sad paths, and exempt-list logic are all covered. Pushing to 100% is a follow-up worth filing if BL6+ scrutiny demands it.

## Tooling state

- ruff check + format — clean
- mypy --strict on `src/` (20 files) — clean
- semgrep p/owasp-top-ten + project custom rules — 0 findings
- gitleaks — 0 findings

## Decision

**BL5 FastAPI skeleton is cleared to advance through the Build Loop** after the F1 fix pass (committed inline). One SEV-3 finding (token field-name redaction conflict) found and fixed inline; one SEV-4 information item (streaming-cap unauthenticated edge) accepted with documented rationale and direct unit-test coverage. No SEV-1 or SEV-2 findings.

Defense-in-depth across the threat model (TM-001 token leak, TM-012 log redaction, TM-013 fingerprinting via middleware-as-auth, TM-018 memory bomb, TM-023 OQ2 loopback enforcement) is verified by 48 tests + automated SAST.

## Follow-up tracking

- SEV-4 — `pool.py` branch coverage (BL4 carry-over #42); BL5 coverage gap is much smaller (96% vs. 81%) but the same long-tail
- SEV-4 — body-cap streaming integration test once a body-consuming endpoint ships in BL6+

## Sign-off

- Implementation: `<green-phase commit hash>` (this commit)
- Test suite: 48 tests passing in `tests/api/`, 96% branch coverage
- Full project suite: 329 tests passing
- Ruff + mypy --strict + semgrep + gitleaks all clean
