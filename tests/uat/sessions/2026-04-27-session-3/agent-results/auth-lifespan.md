# UAT-3 Auth + Lifespan Audit
**Agent:** auth-lifespan
**Date:** 2026-04-27
**Persona:** Senior Security Engineer
**Scope:** BL5 FastAPI skeleton — `src/orchestrator/api/{main,middleware,dependencies,routers/health}.py` + `src/orchestrator/db/pool.py` lifespan callees.
**Reference:** ADR-0012 (D2 bearer auth, D6 fail-fast 503), TM-001, TM-013, Bible §7.3, §9.2.

---

## A: hmac.compare_digest

### Walk
The relevant code lives in `BearerAuthMiddleware.__call__` at `src/orchestrator/api/middleware.py:222–238`:

```python
settings = get_settings()
expected = settings.orchestrator_token.get_secret_value()
if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
    sha = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    _log.warning("api.auth.rejected", reason="bad_token", path=path, rejection_fingerprint=sha)
    await self._send_401(send)
    return
```

1. **Both arguments are bytes.** Both `token` and `expected` are explicitly `.encode("utf-8")`. Type alignment OK; CPython's `hmac.compare_digest` documented to require matching types (str vs bytes mix raises TypeError) — both bytes here. Verdict: correct.

2. **Token length variations:**
   - `len 0` — handled by an earlier guard at line 217 (`if not token: ... 401 return`); never reaches compare_digest. OK.
   - `len 1`, `len 32`, `len 64`, `len 4096` — `compare_digest` operates in time proportional to the LONGER of the two inputs and signals length-mismatch via a non-secret side channel (bool result), which is acceptable per the spec. No crash possible. Note: if the attacker submits a 4096-byte token, the comparison still runs in O(4096) time but does NOT leak which prefix matched — only that `len(submitted) != len(expected)` (or that they differ somewhere). This is the documented limitation of `compare_digest`; not a finding.

3. **Operator misconfig — trailing whitespace on configured token:** Settings (`src/orchestrator/core/settings.py:96–104`) installs a `mode="before"` validator that strips whitespace from the SecretStr. So a configured token with `"abc...  \n"` is normalized to `"abc..."` BEFORE the constant-time comparison. Verdict: fail-closed if attacker also sent trailing whitespace; the stripped configured value won't match an attacker-submitted token that retains whitespace. Conversely, an attacker who pads with whitespace gets stripped at attacker side via line 216 (`auth_header[len("Bearer ") :].strip()`). Both sides strip — symmetric, no oracle. OK.

4. **Could a different early-return / exception path leak existence-of-token-vs-not-configured?**
   - `get_settings()` is cached (`@lru_cache`) and the orchestrator_token field is **required** (no default) with min_length=32 enforced in `_check_token_length` (line 106–127). If the token isn't configured, the *entire app* fails to construct (settings construction raises) and uvicorn never serves traffic. Therefore the "configured token absent" state at runtime is unreachable for unauth endpoints.
   - There is NO codepath where compare_digest is skipped after a header is presented. The only skip paths (OPTIONS, exempt prefix) happen BEFORE the header is read. OK.

5. **Is the rejection_fingerprint computed only after compare_digest fails?** YES — line 224 runs the comparison; the SHA256 prefix at line 225 is computed only inside the `if not compare_digest(...)` branch. No timing tell from fingerprint computation on the success path. OK.

6. **Subtle observation — log emission timing:** On the rejection path the middleware emits a structlog warning BEFORE calling `_send_401`. structlog's processor chain (especially JSON renderer) is non-trivial and adds a measurable, reproducible latency to the rejection path that is NOT present on the success path. **This is a marginal timing oracle.** A network attacker who can sample many authenticated and unauthenticated requests could distinguish "auth-rejected" from "auth-accepted-but-route-404" purely from response-latency distribution. However, since the success path also does `await self.app(...)` (downstream work, possibly more expensive than logging), the actual sign of the latency delta is path-dependent. Calling this out as **SEV-4 informational**, not actionable for MVP.

### Verdict
`hmac.compare_digest` usage is **correct**. Both arguments bytes, both whitespace-stripped, fingerprint computed only on miss, no exception-class oracle. No SEV-1/2/3 in this section.

---

## B: Loopback enforcement (OQ2)

### Walk
`src/orchestrator/api/middleware.py:241–252`:

```python
if any(p.match(path) for p in LOOPBACK_ONLY_PATTERNS):
    client_info = scope.get("client")
    client_host = client_info[0] if client_info else None
    if client_host != "127.0.0.1":
        _log.warning("api.auth.rejected", reason="non_loopback", ...)
        await self._send_403(send)
        return
```

`LOOPBACK_ONLY_PATTERNS = (re.compile(r"^/api/v1/platforms/[^/]+/auth$"),)` (`dependencies.py:30`).

1. **Source of truth:** `scope.get("client")` — the ASGI peer tuple. NOT `X-Real-IP`, NOT `X-Forwarded-For`. **Correct.** This is exactly what TM-023's mitigation requires. OK.

2. **Defensive against `scope["client"] is None`:** YES — `scope.get("client")` + `if client_info else None` handles None. The eventual comparison `None != "127.0.0.1"` evaluates to True → 403 returned. **Fail-closed.** OK.

3. **IPv6 loopback:** **FINDING.** uvicorn binds `127.0.0.1` by default per `settings.api_host` so this is not exploitable in the shipped configuration, BUT:
   - If the operator configures `api_host = "::"` or `api_host = "::1"` (dual-stack), the local connection from `::1` (v6 loopback) presents `client_host == "::1"`, which fails `!= "127.0.0.1"` and is **rejected with 403**. This is fail-closed (correct security stance) but also functionally broken — an operator who runs IPv6-only would have **all platform auth requests fail**.
   - More concerning: an attacker connecting from `::ffff:127.0.0.1` (IPv4-mapped IPv6) on a dual-stack listener presents `client_host == "::ffff:127.0.0.1"`, which **fails the equality check** → also 403. Fail-closed; not a bypass.
   - **The bypass risk is the inverse:** a request from `127.0.0.1` (the literal string) is what the check looks for. If a future deployment runs behind a reverse proxy on the same host that presents the upstream as `127.0.0.1` to uvicorn, ANY request that traverses that proxy would pass the loopback check — even from external IPs. The current TM-023 narrative assumes uvicorn is bound to 127.0.0.1 directly. The check has no defense-in-depth against an operator who later puts a proxy in front. SEV-3 — **operator footgun**.

4. **X-Forwarded-For trust:** **NOT consulted.** Source of truth is `scope["client"]`. Correct per TM-023. OK.

5. **Deny-by-default for new loopback paths:** **FINDING.** `LOOPBACK_ONLY_PATTERNS` is an explicit allowlist of paths that get the EXTRA loopback check. Any new path added in BL6+ that should be loopback-only requires the developer to remember to update the regex tuple. There is **no inversion** (e.g. "platforms/auth always requires loopback by some annotation on the route") — the regex is the only enforcement seam. If a future BL adds `POST /api/v1/admin/rotate-token` and forgets the regex update, it's reachable from non-loopback. **SEV-2.**

### Verdict
- The check itself is correct (uses scope["client"], fail-closed when client is None, doesn't trust X-Forwarded-For).
- **SEV-2:** opt-in allowlist pattern is forget-prone. Recommend a route-decorator-driven enforcement OR a deny-by-default policy with explicit allowlist of NON-loopback paths within `/platforms/`.
- **SEV-3:** No documented warning that putting a reverse proxy on the same host would defeat the check.
- IPv6 functional gap is also SEV-3 (correctness, not exploit).

---

## C: AUTH_EXEMPT_PREFIXES correctness

### Walk
`src/orchestrator/api/middleware.py:199`:

```python
if any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
    await self.app(scope, receive, send)
    return
```

`AUTH_EXEMPT_PREFIXES = ("/api/v1/health", "/api/v1/openapi.json", "/api/v1/docs", "/api/v1/redoc")`.

1. **Match logic:** `str.startswith` — **prefix match, NOT anchored regex with terminator.**

2. **Sub-string bypass scenarios:**
   - `/api/v1/healthcheck` — would match `startswith("/api/v1/health")`. Today no such route exists, so a bare 404 is returned (after middleware passes through the auth-bypass branch). The 404 handler runs without auth. **Today: not exploitable** (no leak content). **Future risk:** if BL6+ adds `/api/v1/healthcheckpoint` or `/api/v1/health-debug` thinking it's a separate endpoint, that endpoint will be **silently unauthenticated.** **SEV-2 latent.**
   - `/api/v1/health/internal-debug` — `startswith("/api/v1/health")` is True → bypasses auth. Same latent risk. SEV-2.
   - `/api/v1/openapi.jsonp` — matches `startswith("/api/v1/openapi.json")`. Theoretical only.
   - `/api/v1/docs/secret` — matches `startswith("/api/v1/docs")`. FastAPI mounts docs at `/api/v1/docs` and serves static asset paths under it; an unauthenticated GET to `/api/v1/docs/oauth2-redirect` (a real Swagger asset) is served — that's actually intended. But again, future router-mount under `/docs/` would be silently unauthenticated.

3. **Path-normalization behavior:** ASGI `scope["path"]` is the **already-percent-decoded path** (Starlette/FastAPI decodes before middleware runs). However:
   - `..` segments are NOT collapsed by ASGI/Starlette before middleware sees the path. So `/api/v1/health/../platforms/steam/auth` arrives at middleware as that literal string — does NOT match `startswith("/api/v1/health")` because of the `/../`? **Yes it does match** — `"/api/v1/health/../platforms/steam/auth".startswith("/api/v1/health")` is True. So this path bypasses auth. Whether the downstream FastAPI router resolves it to `/api/v1/platforms/steam/auth` depends on Starlette's router; FastAPI's default router does NOT collapse `..` (it dispatches on the literal path). So the request would 404 at the router. **Today: not directly exploitable** (router doesn't resolve `..`). However, this is a **defense-in-depth gap**: combined with any future component that DOES normalize the path (e.g. an ASGI proxy layer), this becomes an auth bypass. **SEV-3.**
   - Percent-encoded `%2e%2e` likewise — Starlette decodes to `..` before `scope["path"]`, same analysis applies.

4. **Recommendation:** Switch to anchored exact-or-terminator match:
   ```python
   def _is_exempt(path: str) -> bool:
       for p in AUTH_EXEMPT_PREFIXES:
           if path == p or path.startswith(p + "/"):
               return True
       return False
   ```
   Plus an explicit `..`/`%2e` rejection at the top of the middleware (return 400 if path contains `/../` after decoding). Belt-and-braces.

### Verdict
- **SEV-2:** `startswith` admits `/healthcheck`, `/health/debug`, etc. as auth-bypassed if such paths are ever added in future BLs. This is a **latent foot-gun**, not a current bypass (no such endpoints exist).
- **SEV-3:** No explicit rejection of `..` segments; in combination with future path-normalizing infrastructure could be exploited.
- Today no live bypass exists because (a) no offending endpoints, (b) FastAPI router doesn't collapse `..`.

---

## D: Lifespan partial-init cleanup

### Walk
`src/orchestrator/api/main.py:39–72`:

```python
async def _lifespan(app):
    settings = get_settings()
    log = ...
    # 1. migrations (sync; offload)
    try:
        await asyncio.to_thread(migrate.run_migrations, settings.database_path)
    except migrate.MigrationError as e:
        log.critical(...); raise SystemExit(1) from e
    # 2. pool init
    try:
        await init_pool()
    except (SchemaNotMigratedError, SchemaUnknownMigrationError, PoolError) as e:
        log.critical(...); raise SystemExit(1) from e
    # 3. boot metadata
    app.state.boot_time = time.monotonic()
    app.state.git_sha = os.environ.get("GIT_SHA", "unknown")
    log.info("api.boot.complete")
    yield
    log.info("api.shutdown.starting")
    try:
        await close_pool()
    except PoolError as e:
        log.error(...)
    log.info("api.shutdown.complete")
```

1. **Order on startup:** migrations → init_pool → app.state. Failure modes:
   - Migrations fail → `SystemExit(1)` BEFORE pool is initialized → no cleanup needed (pool not created). OK.
   - init_pool fails (one of the listed exceptions) → `SystemExit(1)`. **GAP:** What about exceptions NOT in the catch list? E.g., `aiosqlite.Error` raised mid-`Pool._async_create` that isn't classified as PoolError. Looking at `Pool._async_create`: it has `except BaseException: await pool._teardown_connections(); pool._state="closed"; raise`. So even non-PoolError exceptions get the connections torn down at the Pool layer before propagating. The lifespan's missing catch only means the exception type isn't normalized to SystemExit(1) — it propagates as-is. **Minor consistency issue, not a leak.**
   - app.state assignment failing — `app.state.boot_time = ...` could only fail with truly exotic bugs (state object is a Starlette `State()`; attribute assignment doesn't normally raise). If it did fail, the pool would already be initialized and the lifespan body would propagate the exception WITHOUT calling `close_pool()`. **The yield is not reached, so the cleanup branch after `yield` never runs.** This is a partial-init pool leak. **SEV-3.**

2. **Half-initialized pool from mid-`init_pool`:** `Pool._async_create` opens writer, then loops opening readers. If reader 3 of N fails, the `except BaseException` clause invokes `_teardown_connections()` which closes the writer + every reader in `_reader_pool` (best-effort, swallows individual close errors). **Correctly handled at the BL4 layer.** OK.

3. **30s shutdown timeout vs. slow query:** `close_pool()` calls `asyncio.wait_for(old.close(), timeout=30.0)`. `Pool.close()` calls `_teardown_connections()` which iterates connections and closes them. If a slow query holds a writer connection (writer_lock held by an in-flight handler), `await self._writer.close()` may block until the in-flight `execute()` finishes (aiosqlite uses a thread-per-connection model — close enqueues to that thread). 30s is generous but not unbounded. After 30s: `TimeoutError` → re-raised as `PoolError("close_pool() timed out after 30s")`. Lifespan catches `PoolError` → logs `api.shutdown.pool_close_failed` and continues. **User-visible:** uvicorn shutdown takes up to 30s in the worst case, then logs the failure and exits. Connections may be leaked at the OS level (file descriptors) but the process is exiting anyway. Acceptable. OK.

4. **Double init_pool race / re-entry:** `init_pool` is guarded by `_get_init_lock()` (lazy module-level `asyncio.Lock`). Inside the lock, `if _pool is not None: return _pool`. Idempotent. **However:** `_init_lock` is created on first call as a module-level singleton. In a test reload or worker fork that doesn't reset module globals, the lock is bound to the OLD event loop. `asyncio.Lock()` is created without a loop binding in modern Python (3.10+), but acquiring it from a different loop raises `RuntimeError: ... attached to a different loop`. **Test-environment risk only.** Not exploitable; not SEV-3 in production. SEV-4 informational.

5. **SystemExit(1) propagation:** Per ADR-0012's edge-cases note, `asgi-lifespan` swallows SystemExit in its task wrapper. uvicorn itself, in production, treats a lifespan-startup exception as an exit signal — uvicorn logs `Application startup failed` and shuts down. SystemExit propagates correctly through the native uvicorn runtime. **Correct.** OK.

6. **The migration runner is invoked via `asyncio.to_thread`.** `migrate.run_migrations` is a sync function. If it spawns sub-threads, raises in a sub-thread, or blocks indefinitely, `to_thread` does NOT propagate timeouts. **No timeout on migration step.** If a migration deadlocks, lifespan hangs forever and uvicorn never serves. SEV-3 informational — Bible §10 doesn't mandate a startup-deadline; in single-user single-host deployments, an operator notices.

### Verdict
- **SEV-3:** `app.state.X` assignment between `init_pool()` and `yield` is unguarded — if it raises (exotic), the pool is initialized but `close_pool` is never called. Wrap steps 2–3 in a try/except that calls `await close_pool()` on any exception before re-raising.
- **SEV-3 informational:** Migration runner has no timeout — a broken migration could hang startup indefinitely.
- **SEV-4:** Lifespan catches a closed list of pool-init exceptions; truly unexpected exceptions (e.g. OSError from disk full during pool open) propagate without normalization. Pool-layer teardown is correct, so no resource leak — only the log/exit mode differs.
- BL4 pool's own teardown on partial init is **correct** (BaseException handler in `_async_create`).

---

## E: Differential auth-state response matrix

| State | Status | Headers | Body | Distinguishable from happy path? |
|---|---|---|---|---|
| Token configured absent + no Authorization | App fails to start (settings construction raises before uvicorn binds) — N/A at runtime | — | — | N/A |
| Token configured absent + valid-looking Authorization | Same — N/A | — | — | N/A |
| Token configured + no Authorization, non-exempt path | 401 | `WWW-Authenticate: Bearer realm="orchestrator"`, `Content-Type: application/json`, `X-Correlation-ID: <uuid>` | `{"detail":"unauthorized"}` | YES — distinct status |
| Token configured + malformed Authorization (`Foo bar`) | 401 | same as above | same body | **Same as missing-header** — log reason differs (`malformed_header` vs `missing_header`) but client cannot distinguish. OK |
| Token configured + `Bearer ` (empty token) | 401 | same | same body | Same as missing — client cannot distinguish |
| Token configured + wrong Authorization | 401 | same | same body | **Same as missing/malformed** — client cannot distinguish bad-token from no-token. OK (no enumeration oracle) |
| Token configured + correct Authorization, route exists | 200/2xx | `X-Correlation-ID` only | route body | Distinct — route body |
| Token configured + correct Authorization, route ABSENT | 404 | `X-Correlation-ID`, FastAPI default `Content-Type: application/json` | `{"detail":"Not Found"}` | **Distinct** — and this is the TM-013 concern: a holder-of-token can enumerate routes by 404 vs 2xx. Per ADR-0012 D2, this is by design and acceptable because the token holder is already trusted. OK |
| Loopback-only path (`/platforms/X/auth`) from non-loopback IP, no Authorization | 401 (NOT 403) | WWW-Authenticate, etc. | `{"detail":"unauthorized"}` | **Same as any 401** — does NOT reveal that the path requires loopback. OK |
| Loopback-only path from non-loopback IP, valid Authorization | **403** | `Content-Type: application/json`, X-Correlation-ID | `{"detail":"forbidden: loopback only"}` | **Distinct** — body explicitly says "loopback only". A legitimate-token holder learns "this path requires loopback access." Since they already hold the bearer token, this is acceptable disclosure (not enumeration to outsiders). OK |
| Loopback-only path from loopback IP, valid Authorization, route absent | 404 (BL5: route doesn't exist; BL6+ adds it) | `X-Correlation-ID`, `Content-Type: application/json` | `{"detail":"Not Found"}` | Distinct — but only reachable from loopback w/ valid token. OK |
| Auth-exempt path (`/api/v1/health`) from any IP, no Authorization | 200 or 503 (per D6 fail-fast) | `X-Correlation-ID`, `Content-Type: application/json` | health response (7 fields including `version`, `git_sha`) | **TM-013** — version + git_sha are unauthenticated. Acknowledged in TM-013 mitigation (Phase 3 hardening). OK for MVP |
| Path matching exempt-prefix substring (`/api/v1/healthcheck`, doesn't exist) | 404 (auth bypassed) | `X-Correlation-ID`, JSON | `{"detail":"Not Found"}` | **Distinct from non-exempt 404** — non-exempt 404 returns 401 first. An unauthenticated attacker can probe which paths fall under exempt prefixes by observing 404 vs 401. **SEV-3 enumeration oracle** for the exempt namespace. |

### Most concerning row
**`/api/v1/healthcheck` (or any future sub-path under exempt prefixes)** → 404 instead of 401 from an unauthenticated probe. This lets an attacker map "which routes are publicly exempted" without holding a token. Combined with TM-013's `version`/`git_sha` disclosure, this expands fingerprinting surface. SEV-3.

### Other observations
- **CorrelationIdMiddleware echoes a freshly generated UUID in `X-Correlation-ID`** on 401/403 responses. Useful for forensic correlation; not a leak.
- **`WWW-Authenticate` header on 401** is RFC-compliant (Bible §7.3 implicit); helps ops tooling. Not a leak.
- **`api.auth.rejected` log lines** include `path` for all rejection reasons — operator can see attempted paths. Logged at WARNING (per `_log.warning`); on a busy system this is reasonable telemetry, but on a hostile probe could spam disk. Logging without rate-limit is a low-risk concern given LAN-only trust boundary; SEV-4.

---

## Findings

### SEV-1
None.

### SEV-2

**SEV-2-A: AUTH_EXEMPT_PREFIXES uses unanchored `startswith` — latent auth bypass for future sub-paths under exempt namespaces.**
- **Description:** Bypass logic is `path.startswith(p)` over a tuple of 4 prefixes. Any future path matching a prefix substring (e.g. `/api/v1/healthcheck`, `/api/v1/health/debug`, `/api/v1/docs/admin-secret`) silently bypasses bearer auth.
- **Scenario:** BL8 adds `POST /api/v1/health/reset-counters` for ops convenience without realizing it inherits the exempt status. An unauthenticated LAN attacker hits it → executes the reset.
- **Affected code:** `src/orchestrator/api/middleware.py:199`
- **Fix:** Change to `path == p or path.startswith(p + "/")`. Add a test asserting `/api/v1/healthcheck` returns 401 (currently it would return 404 unauthenticated).
- **Regression test:** Parametrize `tests/api/test_middleware.py::test_exempt_prefix_bypass` over `["/api/v1/healthcheck", "/api/v1/health-extra", "/api/v1/openapi.jsonp", "/api/v1/docs-admin"]` and assert each returns 401 (not 404).

**SEV-2-B: LOOPBACK_ONLY_PATTERNS is opt-in — new privileged endpoints can forget the regex update.**
- **Description:** The list is a regex tuple at `dependencies.py:30`. New BL6+ endpoint that should be loopback-only requires a developer to remember to update the regex. There's no link from the route declaration to the loopback policy.
- **Scenario:** BL10 adds `POST /api/v1/admin/rotate-token` intended to be 127.0.0.1-only; developer forgets `LOOPBACK_ONLY_PATTERNS`. Endpoint is reachable from any LAN host with a valid bearer (or from a Game_shelf compromise — TM-001 chain).
- **Affected code:** `src/orchestrator/api/dependencies.py:29–31` and `middleware.py:241`.
- **Fix options (present to Orchestrator):**
  - **(A)** Add a route-level decorator or `Depends(require_loopback)` so the policy is co-located with the route handler.
  - **(B)** Invert the policy: deny-by-default for `/api/v1/platforms/*/auth` and `/api/v1/admin/*` namespaces, with an explicit allowlist of NON-loopback paths.
  - **(C)** Add a CI check (Semgrep / custom pytest) that asserts every `POST /api/v1/platforms/*/auth` and `POST /api/v1/admin/*` route is covered by a regex in `LOOPBACK_ONLY_PATTERNS`.
- **Regression test:** Convention test that walks all registered routes and asserts every `*/auth` POST under `/platforms/` is matched by at least one `LOOPBACK_ONLY_PATTERNS` entry.

### SEV-3

**SEV-3-A: Lifespan partial-init resource leak between `init_pool()` and `yield`.**
- **Description:** Lines 61–63 of `main.py` set `app.state.boot_time` and `app.state.git_sha` AFTER `init_pool()` succeeds. If those assignments raise (or the `os.environ.get` raises), the pool is open but `close_pool` is never called (we never reach `yield`'s cleanup branch).
- **Scenario:** Exotic — if a custom Starlette `State` subclass installed by an operator plugin rejects assignment, or `os.environ` is replaced with a defective mapping. Low likelihood, real consequence (orphan pool, fd leak).
- **Affected code:** `src/orchestrator/api/main.py:60–63`
- **Fix:** Wrap steps 2 (init_pool) and 3 (state assignment) and the `yield` in a try/except/finally that ensures `close_pool()` runs on exception path, OR move the state-assignment BEFORE `init_pool` so a state failure can't leak the pool.
- **Regression test:** Patch `time.monotonic` (or `os.environ.get`) to raise; assert `close_pool` is awaited; assert no aiosqlite connections remain open.

**SEV-3-B: `..` segments in path are not rejected before `startswith` exempt check.**
- **Description:** `scope["path"]` arrives percent-decoded but with `..` segments intact. `/api/v1/health/../platforms/steam/auth` matches `startswith("/api/v1/health")` → bypasses auth. FastAPI's router does NOT collapse `..`, so today the request 404s — but this is a defense-in-depth gap if any future ASGI middleware (e.g. a future static-file mount, a reverse proxy normalizer) collapses the path.
- **Scenario:** Operator adds an `app.mount("/api/v1/health/static", StaticFiles(...))` for ops dashboards in BL11. `StaticFiles` normalizes `..`. Now `/api/v1/health/../platforms/steam/auth` traverses to `/api/v1/platforms/steam/auth` AND has bypassed auth on its way in.
- **Affected code:** `src/orchestrator/api/middleware.py:199` (no path normalization).
- **Fix:** At top of `BearerAuthMiddleware`, reject paths containing `/../` or `/./` with 400 before any further processing.
- **Regression test:** Assert `/api/v1/health/../foo` returns 400 and never reaches the downstream router.

**SEV-3-C: `LOOPBACK_ONLY_PATTERNS` literal `"127.0.0.1"` comparison fails on dual-stack deployments.**
- **Description:** Equality check `client_host != "127.0.0.1"`. If `api_host` is configured to bind on `::1` or `::`, legitimate loopback IPv6 traffic presents `client_host` as `"::1"` or `"::ffff:127.0.0.1"` and is rejected with 403. Fail-CLOSED (not a bypass) but functionally broken.
- **Scenario:** Operator runs on an IPv6-preferring host. Game_shelf's auth callback never works.
- **Affected code:** `src/orchestrator/api/middleware.py:244`
- **Fix:** Use `ipaddress.ip_address(client_host).is_loopback` with TypeError guard; document in HANDOFF that loopback IPv6 is now accepted.
- **Regression test:** Parametrize over `["127.0.0.1", "::1", "::ffff:127.0.0.1"]` and assert all are accepted; `["192.168.1.5", "10.0.0.1", "0.0.0.0"]` are rejected.

**SEV-3-D: Exempt-prefix vs. non-exempt 404 status differential is an enumeration oracle.**
- **Description:** Unauthenticated probes get 401 on non-exempt missing routes but 404 on missing routes UNDER exempt prefixes. An attacker maps the exempt namespace.
- **Scenario:** Reconnaissance phase before TM-001 token exfil. Attacker does `GET /api/v1/healthx`, `/api/v1/health-foo`, etc. Each 404 confirms the path is under an auth-exempt prefix.
- **Affected code:** `src/orchestrator/api/middleware.py:199` (combined with FastAPI default 404).
- **Fix:** Same as SEV-2-A — anchored prefix match collapses this into "all unauthenticated non-exempt → 401" so only the literal exempt paths admit 404.
- **Regression test:** Same as SEV-2-A.

**SEV-3-E: Migration runner has no startup deadline.**
- **Description:** `await asyncio.to_thread(migrate.run_migrations, ...)` has no timeout. A deadlocked migration hangs lifespan startup forever; uvicorn never accepts traffic but also never exits.
- **Scenario:** Operator's state.db is held by an unrelated process (lsof shows another process has the file). Migration BEGIN IMMEDIATE busy-waits indefinitely.
- **Affected code:** `src/orchestrator/api/main.py:46–50`
- **Fix:** Wrap in `asyncio.wait_for(asyncio.to_thread(...), timeout=60.0)`. On `TimeoutError`: log critical, SystemExit(1).
- **Regression test:** Mock `migrate.run_migrations` with `time.sleep(120)` and assert lifespan raises within ~60s.

### SEV-4 (informational)

**SEV-4-A: Auth-rejection log emission introduces a measurable latency channel.**
- structlog JSON-render on the 401 path adds ~tens of microseconds before `_send_401`. Theoretically distinguishable from happy-path latency by a sufficiently-resourced LAN attacker. No remediation for MVP; LAN-only trust boundary makes this academic.

**SEV-4-B: `_init_lock` module-level singleton can become bound to a stale event loop in test reload scenarios.**
- Production-irrelevant. Test fixtures that recycle event loops should use `reload_pool()` or reset `_init_lock = None`.

**SEV-4-C: `api.auth.rejected` warnings are not rate-limited.**
- A hostile probe can fill logs. LAN-only trust boundary keeps this low-risk; consider rate-limiting (or sampling) at Phase 3 hardening if observability cost matters.

---

## Non-findings

The following items were investigated and **cleared**:

1. **`hmac.compare_digest` argument types** — both bytes, properly encoded. No type-mismatch crash possible.
2. **Token whitespace handling** — settings strips on the configured side; middleware strips on the submitted side. Symmetric, no oracle.
3. **Token min-length enforcement** — Settings rejects <32 char tokens at construction time; configured-token-too-short state is unreachable at runtime.
4. **Rejection fingerprint timing** — computed only on miss; success path doesn't pay the SHA256 cost.
5. **`X-Forwarded-For` trust** — NOT consulted. Loopback check uses `scope["client"]` exclusively.
6. **`scope["client"] is None` defensiveness** — explicit None-guard; falls through to fail-closed 403.
7. **OPTIONS bypass** — correct CORS preflight handling; preflights don't carry credentials so this is safe.
8. **Bad-token vs missing-token differential** — both return identical 401 with identical headers and body. Log reason differs (server-side telemetry only). No enumeration oracle.
9. **Pool partial-init within `Pool._async_create`** — BaseException handler tears down opened connections before propagating. Correct.
10. **`close_pool()` 30s hard timeout** — bounded; logged on exceedance; raises `PoolError`; lifespan catches and continues shutdown logging.
11. **Double `init_pool()` race** — guarded by module lock + idempotent check; safe.
12. **SystemExit propagation** — uvicorn correctly treats lifespan SystemExit as startup failure; asgi-lifespan's swallow is a test-only concern documented in ADR-0012.
13. **OpenAPI security_scheme registration** — documentation-only; middleware does the actual enforcement; no auth-bypass via Swagger UI.
14. **CORS `allow_credentials=False`** — prevents browser cookie/credential exfil even if `allow_origins` were misconfigured. OK.
15. **Health endpoint TM-013 git_sha disclosure** — already acknowledged; Phase 3 hardening planned. Not a regression.
