# BL5 / F9 partial — FastAPI skeleton — Design Spec

**Status:** Accepted (orchestrator-approved 2026-04-27)
**Date:** 2026-04-27
**Phase:** 2 (Construction), Milestone B, Build Loop 5
**Feature:** F9 partial — REST API skeleton on FastAPI :8765
**Builds on:** BL3 (ID4 Settings), BL4 (DB pool), BL2 (ID3 logging), BL1 (ID1 migrations)
**Cross-references:** Bible §3.3 (stack), §7.3 (auth), §8 (observability), §8.4 (health), §9.2 (REST API)

<!-- Last Updated: 2026-04-27 -->

## 1. Purpose & scope

Ship the *skeleton* of the F9 REST API — enough surface for BL6+ feature endpoints to land cleanly, no more. After BL5:

- The orchestrator boots into a working FastAPI app on `:8765` (uvicorn invocation: `uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765`).
- Lifespan startup runs migrations, initializes the BL4 pool singleton.
- Three custom middlewares enforce correlation-ID propagation, 32 KiB body cap, bearer-auth + 127.0.0.1 enforcement (OQ2).
- `CORSMiddleware` (Starlette built-in) handles cross-origin policy from `settings.cors_origins`.
- `/api/v1/health` returns the 7-field response per Bible §8.4 with correct 503/200 semantics.
- OpenAPI schema and Swagger UI are exempt from auth (per FastAPI convention; see §3.3 for the threat-model rationale).

**Explicitly out of scope (BL6+):**
- Game / Job / Manifest / Stats / Platforms endpoints (the other 11 of the 12 endpoints in Bible §9.2).
- The Click CLI integration with this app surface.
- The single-file HTML status page (F10).
- Bearer-token rotation runbook (Phase 4 HANDOFF.md).

## 2. Locked decisions (7 brainstorm questions)

Questions resolved 2026-04-27 in design dialogue with orchestrator:

| # | Topic | Decision | Rationale |
|---|---|---|---|
| Q1 | App layout | **Hybrid:** `api/main.py` (app factory + middleware + lifespan) + `api/dependencies.py` + `api/middleware.py` + `api/routers/health.py` | BL6+ scales by adding router files; main.py stays small. Single-file would balloon to 1500+ LoC by BL12. Per-feature splitting too early dilutes review. |
| Q2 | Bearer-auth | **ASGI middleware** (not `Depends(security)`) | TM-013 fingerprinting defense — middleware runs on every request including 404s. Dependency wouldn't fire on 404s, leaking version/state. Trade-off: OpenAPI security_scheme registered manually; Swagger UI Authorize button still works via FastAPI's `openapi_security_schemes` config. |
| Q3 | Pool exposure | **`Depends(get_pool_dep)`** wrapping module singleton `orchestrator.db.pool.get_pool()` | Idiomatic FastAPI; `app.dependency_overrides[get_pool_dep] = test_pool` for clean test isolation. Mirrors planned `Depends(get_settings)` pattern. |
| Q4 | Test client | **`httpx.AsyncClient` + `httpx.ASGITransport(app=app)`** | Project is async-native; sync TestClient fights the grain. Verified via Context7: `ASGITransport(app=app, client=("ip", port))` lets us simulate non-loopback origins for OQ2 testing — matches our threat-model exactly. |
| Q5 | Correlation-ID | **ASGI middleware** entering ID3's `request_context()` | Cross-cutting; must wrap auth + body-cap so their logs include the CID. Dependency-only would leak: middleware logs would lack CID. |
| Q6 | Health 503 logic | **Fail-fast** — return 503 if ANY of the 7 booleans is false | Bible §8.4 / JQ3 unambiguous. Docker HEALTHCHECK + k8s liveness probe rely on the 503 signal. Always-200 silently breaks deployment-tier contracts. **Implication:** BL5's `/health` returns 503-by-design for `scheduler_running`, `lancache_reachable`, `validator_healthy` (all stub-false until BL6+). Documented in §6.4. |
| Q7 | Body size cap | **ASGI middleware** on Content-Length + streaming-aware byte counter for chunked transfer-encoding | Bible §9.2 specifies global 32 KiB cap. Pure-ASGI required for streaming case (BaseHTTPMiddleware buffers, defeating the streaming check). |

## 3. Module layout

### 3.1 Files

```
src/orchestrator/api/
├── __init__.py
├── main.py              # ~80 LoC — create_app() factory + lifespan + middleware registration + router mounting
├── dependencies.py      # ~50 LoC — get_pool_dep, AUTH_EXEMPT_PREFIXES, LOOPBACK_ONLY_PATTERNS
├── middleware.py        # ~120 LoC — CorrelationIdMiddleware, BodySizeCapMiddleware, BearerAuthMiddleware
└── routers/
    ├── __init__.py
    └── health.py        # ~60 LoC — /api/v1/health handler + HealthResponse Pydantic model

tests/api/
├── __init__.py
├── conftest.py                              # ~80 LoC — unit_app, lifespan_app, client/loopback_client/external_client fixtures
├── test_app_factory.py                      # ~5 tests — create_app shape, OpenAPI security scheme, mount points
├── test_lifespan.py                         # ~6 tests — migrations + init_pool + boot_time + shutdown
├── test_middleware_correlation_id.py        # ~8 tests — CID gen / echo / regen / contextvar / response header
├── test_middleware_body_size_cap.py         # ~6 tests — 413 on Content-Length over; 413 on chunked over; under-cap allowed
├── test_middleware_bearer_auth.py           # ~12 tests — exempt paths, malformed header, wrong token, OQ2 loopback enforcement, preflight OPTIONS bypass
└── test_health_endpoint.py                  # ~10 tests — BL5 ship state, status field, uptime, git_sha, cache_volume_mounted, schema drift, extra=forbid
```

Total: ~310 LoC implementation + ~700 LoC test code (matches BL4 ratio).

### 3.2 Total file impact

- 7 new files in `src/orchestrator/api/` (5 source + 2 `__init__.py`)
- 7 new files in `tests/api/` (1 conftest + 6 test files)
- 1 new dev dep: `asgi-lifespan==2.1.0` added to `requirements-dev.txt`
- 0 changes to existing `src/orchestrator/db/` or `src/orchestrator/core/` — BL5 only consumes BL3+BL4 surfaces

### 3.3 Why OpenAPI / Swagger UI is exempt from auth

The OpenAPI schema (`/api/v1/openapi.json`) describes paths and request/response shapes. It does NOT contain secrets, credentials, or pool state. An attacker reading the schema gains nothing they couldn't infer from the public Bible / FRD documents. Gating the schema behind auth would block legitimate operator workflows (Swagger UI → Authorize → call endpoints).

Swagger UI (`/docs`) and ReDoc (`/redoc`) are HTML pages that fetch the schema. Auth-gating them creates a chicken-and-egg: the operator needs Swagger UI to enter the bearer, but Swagger UI is gated behind the bearer. Standard FastAPI convention: docs pages are unauthenticated; the *endpoint calls made through them* are authenticated normally.

TM-013 fingerprinting concern is addressed differently: `/api/v1/health` does not include version-specific fingerprintable data outside what's already in the published Docker image tag. The OpenAPI schema mentions FastAPI version implicitly via response shape but doesn't materially help an attacker who's already at the network boundary.

## 4. Lifespan

### 4.1 Startup sequence

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log = structlog.get_logger()

    # 1. Migrations (sync; offload to thread)
    log.info("api.boot.migrations_starting")
    try:
        await asyncio.to_thread(run_migrations, settings.database_path)
    except MigrationError as e:
        log.critical("api.boot.migrations_failed", reason=str(e))
        raise SystemExit(1) from e

    # 2. Pool init (singleton; verifies schema)
    log.info("api.boot.pool_starting")
    try:
        await init_pool()
    except (SchemaNotMigratedError, SchemaUnknownMigrationError, PoolError) as e:
        log.critical("api.boot.pool_init_failed", reason=str(e))
        raise SystemExit(1) from e

    # 3. Boot metadata for /health
    app.state.boot_time = time.monotonic()
    app.state.git_sha = os.environ.get("GIT_SHA", "unknown")
    log.info("api.boot.complete")

    yield

    # 4. Shutdown — pool's 30s timeout enforced by close_pool()
    log.info("api.shutdown.starting")
    try:
        await close_pool()
    except PoolError as e:
        log.error("api.shutdown.pool_close_failed", reason=str(e))
    log.info("api.shutdown.complete")
```

Failure semantics: `SystemExit(1)` propagates out of lifespan; uvicorn exits cleanly. The container's `restart: unless-stopped` policy handles the retry-loop. This matches Bible §5.4's documented boot sequence.

### 4.2 `app.state` usage

Two read-only fields set during startup and consumed by `/health`:

- `app.state.boot_time: float` — `time.monotonic()` at startup
- `app.state.git_sha: str` — read from `GIT_SHA` env var (Docker build-arg per Bible §11.1), fallback `"unknown"`

The Pool itself is **not** stored in `app.state` — it's the module singleton from BL4 (`get_pool()`). Per Q3, handlers access via `Depends(get_pool_dep)`.

## 5. Middleware

### 5.1 Stack order (outermost first)

```
Request →
  CorrelationIdMiddleware     # enter request_context, attach CID to response
    BodySizeCapMiddleware      # 413 if > 32 KiB
      BearerAuthMiddleware     # 401/403 if not exempt + bad token
        CORSMiddleware (Starlette)
          → FastAPI router → handler
```

Order rationale (load-bearing):
- **CID outermost** so logs from auth/cap include CID
- **Cap before auth** so a 100 MB blob doesn't pre-buffer through auth
- **Auth before CORS** so unauthenticated requests get a clean 401 *with* CORS headers (browsers swallow non-CORS-headered errors)
- **CORS innermost** so OPTIONS preflight (no Authorization header) reaches the CORS handler without auth-rejection

`BearerAuthMiddleware` exempts `OPTIONS` method (preflight) + `AUTH_EXEMPT_PREFIXES`.

### 5.2 `CorrelationIdMiddleware`

```python
_UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)

class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Read incoming X-Correlation-ID; validate as UUID4; else generate fresh.
        headers = dict(scope.get("headers", []))
        cid_header = headers.get(b"x-correlation-id", b"").decode("ascii", errors="ignore")
        cid = cid_header if _UUID4_RE.match(cid_header) else str(uuid.uuid4())

        async with request_context(correlation_id=cid):
            log = structlog.get_logger()
            t0 = time.perf_counter()
            log.info("api.request.received", method=scope["method"], path=scope["path"])

            async def send_with_cid(message: Message) -> None:
                if message["type"] == "http.response.start":
                    response_headers = list(message.get("headers", []))
                    response_headers.append((b"x-correlation-id", cid.encode("ascii")))
                    message = {**message, "headers": response_headers}
                await send(message)

            try:
                await self.app(scope, receive, send_with_cid)
            finally:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                log.info("api.request.completed", duration_ms=duration_ms)
```

Events emitted: `api.request.received` (INFO at start, with `method`, `path`, `correlation_id` via context), `api.request.completed` (INFO at end, with `duration_ms`).

### 5.3 `BodySizeCapMiddleware`

Two paths:

**Path 1 — Content-Length present:**
```python
content_length = int(headers.get(b"content-length", b"0"))
if content_length > BODY_SIZE_CAP_BYTES:  # 32 * 1024
    await self._send_413(send)
    return
```

**Path 2 — Streaming (no Content-Length, e.g. chunked):**
```python
bytes_received = 0
async def receive_with_cap() -> Message:
    nonlocal bytes_received
    msg = await receive()
    if msg["type"] == "http.request":
        body = msg.get("body", b"")
        bytes_received += len(body)
        if bytes_received > BODY_SIZE_CAP_BYTES:
            # Send 413 immediately; further receive() calls won't be processed
            raise BodyTooLargeError()
    return msg
```

`BodyTooLargeError` is caught at the middleware level and converted to a 413 response. Emit `api.body_size_cap_exceeded` (ERROR, with `path`, `content_length` if known, `bytes_received` for streaming case).

### 5.4 `BearerAuthMiddleware`

Decision tree per request:

```
1. Is method == "OPTIONS"?           → skip auth (preflight)
2. Does path match AUTH_EXEMPT_PREFIXES? → skip auth
3. Read Authorization header.
4. Header missing?                    → 401 + WWW-Authenticate: Bearer realm="orchestrator"
5. Header malformed (no "Bearer ")?  → 401
6. token = header[len("Bearer "):]
7. hmac.compare_digest(token.encode("utf-8"), settings.orchestrator_token.get_secret_value().encode("utf-8"))
   - False?                          → 401 (timing-safe)
8. Path matches LOOPBACK_ONLY_PATTERNS AND scope["client"][0] != "127.0.0.1"?
   - True?                           → 403
9. Pass to next middleware.
```

`hmac.compare_digest` on bytes (encoded via UTF-8) — standard Python stdlib pattern. The function is constant-time even on length mismatch; no need for separate length check.

Failure events:
- `api.auth.rejected` (WARNING, with `reason: missing_header | malformed_header | bad_token | non_loopback`, `path`)
- `bad_token` includes `token_sha256_prefix=<first-8-hex>` (TM-012 — never raw token)

### 5.5 `CORSMiddleware` (Starlette built-in)

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,    # JSON list from ORCH_CORS_ORIGINS
    allow_credentials=False,                # bearer-only; no cookie auth
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID"],
)
```

`allow_credentials=False` is intentional: bearer-token auth flows in `Authorization` header, not in cookies. Setting `allow_credentials=True` with `allow_origins=["*"]` is a known CORS footgun; we avoid it by constraint.

## 6. `/api/v1/health` endpoint

### 6.1 Response model

```python
class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    version: str
    uptime_sec: int
    scheduler_running: bool
    lancache_reachable: bool
    cache_volume_mounted: bool
    validator_healthy: bool
    git_sha: str
```

`extra="forbid"` enforced via `ConfigDict` (Pydantic v2 standard).

### 6.2 Handler

```python
@router.get(
    "/health",
    response_model=HealthResponse,
    responses={
        200: {"description": "All subsystems healthy"},
        503: {"description": "At least one subsystem unhealthy", "model": HealthResponse},
    },
)
async def get_health(
    request: Request,
    pool: Pool = Depends(get_pool_dep),
) -> Response:
    pool_health = await pool.health_check()
    schema_status = await pool.schema_status()

    pool_ok = (
        pool_health["writer"]["healthy"]
        and pool_health["readers"]["healthy"] == pool_health["readers"]["total"]
        and schema_status["current"]
    )

    body = HealthResponse(
        status="ok" if pool_ok else "degraded",
        version=__version__,
        uptime_sec=int(time.monotonic() - request.app.state.boot_time),
        scheduler_running=False,            # BL5 stub; real in BL-scheduler
        lancache_reachable=False,           # BL5 stub; real in BL-lancache-self-test
        cache_volume_mounted=Path(get_settings().lancache_nginx_cache_path).is_dir(),
        validator_healthy=False,            # BL5 stub; real in BL-validator
        git_sha=request.app.state.git_sha,
    )

    all_healthy = (
        pool_ok
        and body.scheduler_running
        and body.lancache_reachable
        and body.cache_volume_mounted
        and body.validator_healthy
    )
    return JSONResponse(
        content=body.model_dump(),
        status_code=200 if all_healthy else 503,
    )
```

### 6.3 Status code policy

| Condition | Status code |
|---|---|
| All 5 subsystem booleans true AND pool healthy AND schema current | 200 |
| Any subsystem boolean false | 503 |
| Pool unhealthy or schema drift | 503 |

`status: "ok"` reflects pool-only health; `status: "degraded"` if pool itself is unhealthy. The HTTP status code reflects the overall health (all 7 fields). This split lets future endpoints distinguish "pool is fine but feature X is down" from "pool itself is broken."

### 6.4 BL5 ship-state expectation (DOCUMENTED)

After BL5 lands and `init_pool()` succeeds:
- `pool` fields → all true
- `cache_volume_mounted` → true if `Settings.lancache_nginx_cache_path` exists and is a directory (likely true on operator hardware where Lancache is mounted)
- `scheduler_running`, `lancache_reachable`, `validator_healthy` → **all false (stubbed)**
- → `/health` returns **503** with body `{status: "degraded", scheduler_running: false, ...}`

This is **expected behavior**. Container HEALTHCHECK should fail until BL6+ flips the stubbed booleans. The CHANGELOG entry for BL5 will explicitly note this.

## 7. Test strategy

### 7.1 Fixtures (in `tests/api/conftest.py`)

```python
@pytest_asyncio.fixture
async def unit_app(populated_pool: Pool) -> FastAPI:
    """No lifespan; deps overridden; app.state stubbed."""
    app = create_app()
    app.dependency_overrides[get_pool_dep] = lambda: populated_pool
    app.state.boot_time = time.monotonic()
    app.state.git_sha = "test-sha-deadbeef"
    return app

@pytest_asyncio.fixture
async def lifespan_app(db_path: Path, monkeypatch) -> AsyncIterator[FastAPI]:
    """Real lifespan via asgi_lifespan.LifespanManager."""
    monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
    from asgi_lifespan import LifespanManager
    app = create_app()
    async with LifespanManager(app):
        yield app

@pytest_asyncio.fixture
async def client(unit_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=unit_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c

@pytest_asyncio.fixture
async def loopback_client(unit_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Simulates origin from 127.0.0.1 (for OQ2 testing)."""
    transport = httpx.ASGITransport(app=unit_app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c

@pytest_asyncio.fixture
async def external_client(unit_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Simulates origin from non-loopback IP."""
    transport = httpx.ASGITransport(app=unit_app, client=("192.168.1.100", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
```

The `populated_pool` fixture is reused from `tests/db/conftest.py` (BL4). Path-of-least-resistance: re-export via the `tests/api/conftest.py`.

### 7.2 Test counts and coverage targets

| File | Tests | Coverage focus |
|---|---|---|
| `test_app_factory.py` | ~5 | Middleware order, OpenAPI security_scheme registered, routers mounted, exempt-path constants align with FastAPI's auto-generated paths |
| `test_lifespan.py` | ~6 | Real migrations apply, real pool inits, boot_time/git_sha set, shutdown closes pool, init failure → SystemExit |
| `test_middleware_correlation_id.py` | ~8 | CID generated when missing, echoed when valid UUID4, regenerated when invalid, response header set, structlog contextvar populated, two requests yield distinct CIDs |
| `test_middleware_body_size_cap.py` | ~6 | Content-Length over → 413, Content-Length under → 200, missing CL + chunked over → 413, missing CL + chunked under → 200, GET unaffected by body cap, body_size_cap_exceeded event emitted |
| `test_middleware_bearer_auth.py` | ~12 | Exempt prefixes bypass, missing header → 401 + WWW-Authenticate, malformed (no Bearer) → 401, wrong token → 401, correct token → handler runs, OPTIONS preflight bypass, OQ2 loopback enforcement (loopback_client passes, external_client → 403), token_sha256_prefix logged on bad_token, no raw token in any log line, timing-safe across length variants |
| `test_health_endpoint.py` | ~10 | BL5 ship state returns 503, status field reflects pool state, uptime_sec increases monotonically, git_sha echoes app.state, cache_volume_mounted reflects path existence, schema drift → status="degraded" + 503, pool unhealthy → status="degraded" + 503, HealthResponse extra=forbid rejects unknowns, model_dump shape matches spec, response Content-Type is application/json |

**Total: ~47 tests, target ≥ 95% branch coverage on `src/orchestrator/api/`** (the BL4 81%-coverage gap is filed as #42; BL5 aims tighter to set the precedent for the API layer).

### 7.3 Slow tests / deferred to UAT-3

None for BL5 — no sustained-workload assertions yet. The first slow test for the API layer will be Spike F-style load on `/health` p99 < 50 ms idle (Bible §9.2 SLO), which lands when there's enough surface to load-test meaningfully (BL-validator timeframe).

## 8. PRAGMA / Settings additions

**None required.** BL5 consumes existing Settings fields:
- `orchestrator_token` (BL3)
- `api_host` (BL3)
- `api_port` (BL3)
- `cors_origins` (BL3)
- `database_path`, `pool_*`, `db_*` (BL3 + BL4 addendum)
- `lancache_nginx_cache_path` (BL3)

No new fields. No new diagnostic warnings. ADR-0010 unchanged.

## 9. Documentation deliverables

- **ADR-0012** — DB pool architecture decision record (8 decisions table from §2 + cross-references)
- **CHANGELOG.md** — `[Unreleased]` entries under Added (FastAPI app, /health endpoint, CORS), Security (bearer-auth middleware with timing-safe compare, body-cap middleware with streaming variant, OQ2 loopback enforcement)
- **FEATURES.md** — Feature 5 (BL5 — FastAPI skeleton)
- **README.md** — startup section: `uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765`. Note that BL5 `/health` returns 503 by-design (3 stubbed subsystems).
- **PROJECT_BIBLE.md** — §3.2 sub-ADR list (add ADR-0012), §9.2 status field added "Last Updated: 2026-04-27" (the API spec section is unchanged but reads cleaner with a recent date).

## 10. Follow-up issues (file at BL5 close)

Anticipated SEV-3/SEV-4 items per the spec self-review and per memory of BL3+BL4 follow-up patterns:

1. **SEV-3** — Spike F load-test integration on `/health` p99 (deferred until validator + scheduler exist)
2. **SEV-4** — `api.request.completed` events should include `correlation_id` automatically via structlog contextvar (verify ID3's `_redact_sensitive_values` doesn't strip the CID)
3. **SEV-4** — Streaming body-cap test needs hypothesis property test (extends #39 / #41 follow-up scope)
4. **SEV-4** — OpenAPI security_scheme documentation: extend FastAPI's auto-generated docs with a security-scheme description block

## 11. Memory artifact (qdrant-store at BL5 close)

Mirror BL3 / BL4 patterns. Save `project_bl5_fastapi_skeleton_complete.md`:
- 7 locked decisions (one-line each)
- Build Loop commit hashes
- Total LoC + test count + coverage achieved
- Non-obvious learnings discovered during execution (especially: any Context7 findings during plan/implement that contradict this spec)
- Follow-up issue numbers
- Pointer to ADR-0012

## 12. Commit plan

Anticipated 6-commit sequence on `feat/bl5-f9-skeleton` branch (each gets A/B/C structure approval per established rhythm):

1. `docs(spec): BL5 FastAPI skeleton — 7 decisions, ASGI middleware stack` — this spec
2. `docs(plan): BL5 FastAPI skeleton — N-task implementation plan` — writing-plans output
3. `chore(deps): add asgi-lifespan==2.1.0 to dev deps` — single dep bump
4. `test(api): BL5 FastAPI skeleton — failing test suite (~47 tests, 6 files)` — TDD red phase
5. `feat(api): BL5 FastAPI skeleton — main + middleware + dependencies + health router` — green phase
6. `docs(adr,changelog,features): BL5 — ADR-0012 + Feature 5` — final docs

Plus precursor commit if any unforeseen Settings/DB needs surface.

## 13. Definition of done

- [ ] All ~47 tests pass (default `pytest`)
- [ ] ≥ 95% branch coverage on `src/orchestrator/api/`
- [ ] Real lifespan path exercised by ≥ 1 integration test
- [ ] OQ2 loopback enforcement covered (positive + negative)
- [ ] No raw token in any log line under any failure path
- [ ] Ruff + mypy --strict + semgrep + gitleaks all clean
- [ ] ADR-0012 + CHANGELOG entries + FEATURES Feature 5 + README startup section + PROJECT_BIBLE §3.2 update committed
- [ ] PR opened (per `feedback_pr_merge_ownership.md`, user merges)

## 14. Self-review

**Placeholder scan:** None — no TODOs / TBDs in this spec. ADR-0012 number is reserved (next sequential after ADR-0011 from BL4).

**Internal consistency:**
- Decision Q1 says hybrid (separate routers/health.py) and §3.1 lists `routers/health.py` — consistent.
- Decision Q2 says middleware (not Depends), §5.4 implements as middleware, §3.3 documents the auth-exempt-paths rationale — consistent.
- Decision Q6 says fail-fast 503-on-any-false; §6.3 implements that policy; §6.4 documents the BL5 ship-state expectation that it returns 503 — consistent and explicit.

**Ambiguity:**
- The phrase "preflight OPTIONS bypass" in §5.4 could be misread as "ALL OPTIONS bypass auth." Clarified: the middleware skips auth on OPTIONS regardless of path; CORS handles the preflight from there. This matches Starlette CORSMiddleware's expectations.
- "BL5 ship-state returns 503" could be misread as a bug. §6.4 explicitly labels it "expected behavior" with a CHANGELOG note commitment.

**Scope check:**
- Single coherent feature (FastAPI skeleton). 47 tests is at the upper end of "single BL" but appropriate given S-2 scope (3 custom middlewares × 6-12 tests each + lifespan + health). Not over-scoped.
- No design decisions deferred. Every technical question from the brainstorm is locked.

Spec is shippable as-is.

## 15. References

- Bible §3.3 (stack), §7.3 (auth), §8 (observability), §8.4 (health), §9.2 (REST API)
- BL3 ID4 ADR-0010 (Settings module — primary consumer)
- BL4 ADR-0011 (DB pool architecture — primary consumer)
- ID3 BL2 ADR-0009 (Logging framework — `request_context()` integration)
- Threat model: TM-001 (token leak), TM-005 (SQL injection — covered by pool layer), TM-012 (log redaction), TM-013 (fingerprinting — middleware-as-auth defense), TM-018 (memory bomb — body-size cap), TM-023 (kill chain — OQ2 loopback enforcement)
- Context7 verifications captured during design (2026-04-27): FastAPI middleware patterns, httpx.ASGITransport API, pytest-asyncio asyncio_mode, asgi-lifespan canonical pattern
- FastAPI docs (via Context7 `/fastapi/fastapi`): lifespan asynccontextmanager, ASGI middleware via add_middleware, CORSMiddleware
- httpx docs (via Context7 `/encode/httpx`): ASGITransport with `client=` parameter for simulated client address
