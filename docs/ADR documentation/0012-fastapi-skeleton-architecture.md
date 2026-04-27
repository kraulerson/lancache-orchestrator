# ADR-0012: FastAPI Skeleton Architecture — Hybrid Module Layout, Pure-ASGI Middleware Stack, Lifespan-Managed Pool

**Status:** Accepted
**Date:** 2026-04-27
**Phase:** 2 (Construction), Milestone B, Build Loop 5 (BL5-F9-fastapi-skeleton)
**Related:** ADR-0001 (Orchestrator Architecture), ADR-0008 (Migration Runner), ADR-0009 (Logging Framework), ADR-0010 (Settings Module), ADR-0011 (DB Pool)
**Feature:** BL5 — F9 partial (FastAPI skeleton)

<!-- Last Updated: 2026-04-27 -->

## Context

BL5 ships the substrate every Milestone B+ feature endpoint will live on. After BL5:

- `uvicorn orchestrator.api.main:create_app --factory` boots the app on port 8765.
- A 4-layer middleware stack (CorrelationId → BodySizeCap → BearerAuth → CORS) handles cross-cutting concerns.
- `@asynccontextmanager` lifespan runs migrations + initializes the BL4 pool singleton; shutdown closes the pool with the BL4-defined 30s hard timeout.
- `/api/v1/health` is the only endpoint, returning the 7-field response per Bible §8.4.
- BL6 adds the first feature endpoints (likely platforms / library / games) on this substrate.

Project Bible §3.3 pre-commits to FastAPI + uvicorn[standard] + pydantic v2. Bible §7.3 commits to bearer-token auth with `hmac.compare_digest` (timing-safe) and `127.0.0.1`-only enforcement on `POST /api/v1/platforms/{name}/auth` (OQ2). Bible §8.4 specifies the health-endpoint shape verbatim (7 fields). Bible §9.2 specifies the API contract (12 endpoints under `/api/v1`, request size cap 32 KiB, OpenAPI bearer-gated, `extra="forbid"` on every Pydantic model). Bible §10.3 commits to no f-string SQL via Semgrep (irrelevant in API layer; the pool layer enforces).

The live questions for BL5 were: app layout (single-file vs. feature-router), bearer-auth implementation (FastAPI Depends vs. middleware), pool exposure (Depends vs. app.state vs. import), test client (httpx.AsyncClient vs. TestClient), correlation-ID propagation (middleware vs. dependency), health-endpoint 503 logic (fail-fast vs. always-200), body-size cap (middleware vs. per-endpoint).

A 7-question brainstorm walked through the decision space with A/B/C options. The spec (`docs/superpowers/specs/2026-04-27-bl5-fastapi-skeleton-design.md`) records the full decision trail with Context7 verifications.

This ADR records the load-bearing architectural decisions plus the SEV-3 finding (token-field-name conflict with ID3's auto-redaction) surfaced and fixed during the green-phase test pass.

## Decisions

### D1 — Hybrid app layout (main.py + dependencies.py + middleware.py + routers/)

**Context:** With 12 future endpoints under `/api/v1`, a single-file layout would balloon `main.py` to 1500+ LoC by BL12; a feature-router-from-day-one pattern over-splits at the skeleton stage and dilutes review (most BL5 content is centrally relevant).

**Decision:** Hybrid. `src/orchestrator/api/main.py` houses the `create_app()` factory, `_lifespan()` async context manager, middleware registration, and OpenAPI security_scheme injection. `src/orchestrator/api/dependencies.py` exposes module-level constants (`AUTH_EXEMPT_PREFIXES`, `LOOPBACK_ONLY_PATTERNS`, `BODY_SIZE_CAP_BYTES`, `__version__`) and the `get_pool_dep` FastAPI dependency. `src/orchestrator/api/middleware.py` is the home for all custom ASGI middlewares. `src/orchestrator/api/routers/health.py` is the first feature router; BL6+ adds `routers/platforms.py`, `routers/games.py`, etc.

**Consequence:** BL6+ scales by adding router files without touching `main.py`. The natural seams (app config / shared deps / endpoints) match FastAPI community conventions. Module-level constants make middleware testable in isolation (no need to construct the full app).

### D2 — Bearer auth as ASGI middleware (not FastAPI Depends)

**Context:** TM-013 fingerprinting concerns: a 404 on a non-existent path can leak version-specific hints to an attacker. If bearer auth runs as a FastAPI `Depends` dependency, it only fires on routes that declare it — 404 paths bypass auth entirely, returning a clean 404. With auth as middleware, 404 paths *also* require auth (because middleware fires unconditionally), returning 401 — which doesn't distinguish "endpoint exists but you're unauth'd" from "endpoint doesn't exist."

**Decision:** Bearer auth implemented as pure-ASGI middleware (`BearerAuthMiddleware` in `middleware.py`). Method-level `OPTIONS` bypass (CORS preflight) and path-prefix exempt list (`AUTH_EXEMPT_PREFIXES`: `/api/v1/health`, `/api/v1/openapi.json`, `/api/v1/docs`, `/api/v1/redoc`). All other paths — including 404s — get auth-checked. `hmac.compare_digest` on UTF-8-encoded bytes for timing-safe comparison. Path matches `LOOPBACK_ONLY_PATTERNS` (regex `^/api/v1/platforms/[^/]+/auth$`) get the additional 127.0.0.1 check (OQ2). OpenAPI security_scheme registered manually in `create_app()` so Swagger UI's Authorize button works.

**Consequence:** Verified by tests `test_correct_token_passes_auth` (auth passes → 404 because route absent) and the 7 rejection-path tests in `TestBearerAuthRejection`. TM-023 mitigation (kill chain through platforms/auth) is in place even though the actual handler doesn't exist in BL5 — the middleware enforces correctly when BL6+ adds the route.

### D3 — Pool exposure via `Depends(get_pool_dep)` (not app.state, not direct import)

**Context:** Three patterns coexist for accessing application-scoped resources in FastAPI: dependency injection (`Depends`), `request.app.state.X`, or direct module-import. Each has tradeoffs.

**Decision:** `Depends(get_pool_dep)` wrapping `orchestrator.db.pool.get_pool()`. Tests override via `app.dependency_overrides[get_pool_dep] = lambda: test_pool` for clean isolation. The pool itself remains a module singleton (BL4's design); the FastAPI dependency is just a thin async wrapper around `get_pool()`.

**Consequence:** Handlers declare `pool: Pool = Depends(get_pool_dep)` in their signatures, making the dependency explicit. Test fixtures override the dependency without touching `app.state` or monkeypatching the module. Mirrors the planned `Depends(get_settings)` pattern that BL6+ will likely adopt for Settings access in handlers.

### D4 — `httpx.AsyncClient` + `httpx.ASGITransport` for tests; `asgi-lifespan` for lifespan integration tests

**Context:** Project is async-native (every `tests/db/` and `tests/core/` test is `async def`); the sync `fastapi.testclient.TestClient` would force awkward `asyncio.to_thread` workarounds. Tests need to simulate non-loopback origins for OQ2 testing (no real socket required, but `client.host` matters).

**Decision:** Default to `httpx.AsyncClient(transport=httpx.ASGITransport(app=unit_app))`. For OQ2-positive tests, pass `client=("127.0.0.1", 12345)` to `ASGITransport`. For OQ2-negative tests, pass `client=("192.168.1.100", 54321)`. For tests that need to exercise the real lifespan path (migrations apply, pool init, app.state populated), wrap the app in `asgi_lifespan.LifespanManager(app)` — without this, `httpx.AsyncClient` does NOT trigger lifespan events (Context7-verified: FastAPI docs explicitly call this out).

**Consequence:** 95% of tests use the fast `unit_app` fixture (no lifespan, deps overridden, app.state stubbed). 5% of tests use the slower `lifespan_app` fixture for true integration coverage. `asgi-lifespan==2.1.0` added to `requirements-dev.txt` (small surface, single-purpose, FastAPI-recommended).

### D5 — Correlation-ID propagation via outermost ASGI middleware

**Context:** ID3 (BL2) ships `request_context()` — a sync `@contextmanager` using structlog's token-based contextvar reset. The CID needs to wrap every log emission during request processing, including logs from cap-check and auth-check middlewares positioned closer to the application.

**Decision:** `CorrelationIdMiddleware` is the OUTERMOST layer of the middleware stack. Reads incoming `X-Correlation-ID` header (UUID4 regex check; regenerates if missing or invalid). Enters `with request_context(correlation_id=cid)` for the lifetime of the request. Echoes the CID in the response header. Emits `api.request.received` (INFO at start) and `api.request.completed` (INFO at end with `duration_ms`).

**Consequence:** Every log line from request processing — including `api.body_size_cap_exceeded`, `api.auth.rejected` — carries the correlation_id automatically (structlog contextvar). Operators can grep the JSON log for a specific CID and see the full request trace from receive to response. `api.request.completed`'s `duration_ms` lays the groundwork for Post-MVP Prometheus metrics per Bible §8.5.

### D6 — Fail-fast 503 health policy (Bible §8.4 / JQ3)

**Context:** Bible §8.4 specifies "Returns 503 if any boolean is false (JQ3)." Two implementations were considered: fail-fast (return 503 immediately if any subsystem is unhealthy) vs. always-200 (return body with health booleans, let consumer decide). The trade-off: fail-fast aligns with Docker `HEALTHCHECK` and k8s `livenessProbe` semantics; always-200 is simpler but silently breaks deployment-tier health contracts.

**Decision:** Fail-fast. `/health` returns 503 if ANY of the 7 subsystem fields is false. The body still includes the full 7-field response so the operator can see exactly which subsystem caused the 503.

**Consequence (BL5 ship state):** `/health` returns **503 by-design** because three subsystems (`scheduler_running`, `lancache_reachable`, `validator_healthy`) are stub-false until BL6+ ships them. This is documented in CHANGELOG, README, and FEATURES — operators reading those should expect 503 during the BL5→BL12 transition window. Container HEALTHCHECK and k8s probes will report unhealthy until the stubs flip; that's correct behavior.

The `status` field (a separate `Literal["ok", "degraded"]`) reflects pool-only health. Future endpoints will use `status` to distinguish "pool is fine but feature X is down" from "pool itself is broken."

### D7 — 32 KiB body cap via streaming-aware ASGI middleware

**Context:** Bible §9.2 specifies a global 32 KiB request body cap. TM-018 (manifest memory bomb) wants the cap enforced before any handler reads the body. Two paths to consider: requests with `Content-Length` (proactive check on the header) and chunked / no-Content-Length (streaming check via `receive()` interception).

**Decision:** Pure-ASGI middleware (`BodySizeCapMiddleware`) — required because `BaseHTTPMiddleware` buffers the entire request body before passing to next layer, defeating the streaming check (FastAPI release-notes documents this around v0.106). Two-path implementation: Content-Length present and over-cap → immediate 413 (no receive() called). Streaming/chunked → wrap `receive()` to track accumulated bytes; raise `_BodyTooLargeError` at first read past cap; convert to 413 response.

**Consequence:** Authenticated handlers that consume request bodies are protected by the streaming cap (verified by direct middleware unit test in `test_chunked_oversize_rejected_413_via_direct_middleware`). The Content-Length proactive path applies to all requests including unauthenticated. The streaming path is best-effort for unauthenticated requests because bearer-auth (correctly positioned inside the cap layer) rejects before `receive()` is called — but this is moot because no body buffering occurs in that case.

## Edge cases (acknowledged, lived with)

- **`request_context` is sync, not async.** ID3 (BL2) shipped it as `@contextmanager` returning `Iterator[str]`. The middleware uses `with request_context(...)` — NOT `async with`. Caught during green-phase mypy run; fixed before commit.
- **`asgi-lifespan` swallows `SystemExit` in its task wrapper.** The lifespan-failure test bypasses asgi-lifespan and invokes `app.router.lifespan_context(app)` directly to catch the raw SystemExit. Documented inline.
- **BL5 has no body-consuming endpoint**, so the streaming cap can't be HTTP-tested. Direct middleware unit test with a fake downstream app fills the gap. BL6+ integration tests will exercise it via real endpoints.
- **`token_sha256_prefix` field name** would have been auto-redacted by ID3's `_redact_sensitive_values` (it matches the `token` keyword). Renamed to `rejection_fingerprint` (semantically identical, no overlap with sensitive-key regex). Caught by `test_auth_rejected_event_emits_with_sha256_prefix` during green-phase iteration.
- **OpenAPI `bearerAuth` security_scheme is registered for documentation purposes only** — middleware does the actual enforcement. The scheme makes Swagger UI's Authorize button work; FastAPI doesn't auto-enforce it (which would conflict with our middleware-based enforcement).

## Cross-references

- **Spec:** `docs/superpowers/specs/2026-04-27-bl5-fastapi-skeleton-design.md` (full decision trail with A/B/C tradeoffs + Context7 citations)
- **Plan:** `docs/superpowers/plans/2026-04-27-bl5-fastapi-skeleton.md` (23-task implementation plan)
- **Audit:** `docs/security-audits/bl5-f9-fastapi-skeleton-security-audit.md` (Phase 2.4 self-audit)
- **Settings consumer:** ADR-0010 + addendum
- **Pool consumer:** ADR-0011

## References

- Bible §3.3 (stack), §7.3 (auth), §8 (observability), §8.4 (health), §9.2 (REST API)
- Threat model: TM-001 (token leak), TM-005 (SQL injection — N/A in API layer), TM-012 (log redaction), TM-013 (fingerprinting), TM-018 (memory bomb), TM-023 (kill chain — OQ2 enforcement)
- Phase 1 ADR-0001 (single-container monolith)
- Context7 verifications captured during design (2026-04-27): FastAPI middleware patterns + lifespan, httpx.ASGITransport API + `client=` parameter, pytest-asyncio asyncio_mode=auto, asgi-lifespan canonical LifespanManager pattern

## Decision

**Accepted.** Implementation lands in `src/orchestrator/api/` (~315 LoC across 5 files). Test coverage at 96% branches across the API layer; full project suite at 329 tests passing. Phase 2.4 self-audit produced 1 SEV-3 (fixed inline) and 1 SEV-4 information item (accepted with documented rationale).
