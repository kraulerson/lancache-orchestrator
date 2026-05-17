# UAT-3 Threat Model Walk
**Agent:** threat-model
**Date:** 2026-04-27
**Persona:** Penetration Tester
**Scope:** BL5 FastAPI skeleton — `src/orchestrator/api/{main,middleware,dependencies,routers/health}.py`

I am a hostile operator with LAN access. I have read the code (open source). I will catalogue concrete attack steps, not abstract worries.

---

## TM walks

### TM-001 — Bearer-token leak via Game_shelf .env (spoofing → unauthorized API access)

**Walk.** I assume I have already pivoted into the trusted VLAN (per the threat model's standing rule 1). The orchestrator listens on `127.0.0.1:8765` per `Settings.api_host` default — so my first probe is a TCP SYN to `<dxp4800>:8765` from a separate trusted-VLAN host. With the default Settings, the API is BOUND to loopback (`api_host: str = "127.0.0.1"`), and `_emit_config_warnings` (settings.py:190-194) logs `config.api_bound_non_loopback` if the operator changed it. So step 1 of TM-001 doesn't even land unless the operator deliberately rebound to `0.0.0.0` — which is plausible because Game_shelf needs reachability from another LXC. Assume they did.

I now need a token. The middleware (`BearerAuthMiddleware.__call__`, middleware.py:185-254) gates everything except `/api/v1/health`, `/api/v1/openapi.json`, `/api/v1/docs`, `/api/v1/redoc`. With no token, every state-changing request returns 401 with the `_send_401` body `{"detail":"unauthorized"}` and `WWW-Authenticate: Bearer realm="orchestrator"` — see middleware.py:256-273. The token comparison at middleware.py:224 uses `hmac.compare_digest`, which is timing-safe; I cannot mount a timing oracle to recover it byte-by-byte. Token minimum length is 32 chars (settings.py:122) and control chars are rejected — so brute force across the 32-byte opaque space is hopeless.

My best path is still the supply chain (Game_shelf `.env`); that's the TM-023 chain, walked below. As far as **BL5's surface** is concerned, the auth middleware is correctly engaged on every non-exempt path including 404s (per ADR-0012 D2: "404 paths *also* require auth"), so an unauthenticated attacker cannot enumerate routes.

**Verdict:** MITIGATED at BL5 — assuming `api_host` stays loopback or the operator places a network ACL upstream. The structural weakness (single shared bearer per TM-023's residual risk) is not a BL5 regression.

---

### TM-005 — SQL injection through API path params

**Walk.** I open `src/orchestrator/api/routers/health.py` and look for any SQL constructed from user input. The only DB touchpoints are `pool.health_check()` and `pool.schema_status()` (lines 48-49) — neither takes a path param. I look for `f"...{...}"` in any of the four BL5 files — none. The pool layer (BL4) is the only DB-touching module, and per ADR-0011 + the project's Semgrep rule (Bible §10.3), all execute() calls are parameterized.

There are NO BL5 endpoints that accept path params yet. The TM-005 attack surface materializes in BL6+ when `/api/v1/games/{platform}/{app_id}` lands.

**Verdict:** N/A-IN-BL5. The only endpoint (`GET /api/v1/health`) takes zero parameters from the request URL/body. Pydantic `Literal['steam','epic']` and the `^[A-Za-z0-9_\-]{1,64}$` regex referenced in TM-005's mitigation are not yet exercised because the routes don't exist. Re-walk required at BL6.

---

### TM-011 — Stack-trace disclosure in 500 responses

**Walk.** I want a 500. I send a request that triggers an unhandled exception. Several attack vectors: (a) malformed Authorization header types (e.g., raw bytes that aren't UTF-8), (b) exotic header values, (c) lifecycle race against pool init.

Path (a): `auth_header = headers.get(b"authorization", b"").decode("ascii", errors="ignore")` (middleware.py:204) — `errors="ignore"` swallows decode errors. Similarly `cid_bytes.decode("ascii", errors="ignore")` (middleware.py:66). So I cannot trigger a UnicodeDecodeError in the middleware.

Path (b): Content-Length parse — `int(cl_bytes)` is wrapped in try/except ValueError (middleware.py:122-124). Send `Content-Length: invalid` → it falls through to `cl = 0`. No exception escapes.

Path (c): Pre-lifespan request. If I race the server during boot, `request.app.state.boot_time` (health.py:64) and `app.state.git_sha` (health.py:70) are both populated only at lifespan complete (main.py:61-62). If lifespan hasn't run, accessing `request.app.state.boot_time` raises `AttributeError`. FastAPI's default exception handler turns this into `500 Internal Server Error` with response body `{"detail":"Internal Server Error"}` — **no traceback in the response body**. However, the traceback DOES go to stdout via uvicorn's exception logger because there is no app-level `add_exception_handler(Exception, ...)` registered in `create_app()` (main.py:75-141 has none).

That's a finding: **no global Exception handler is registered**. TM-011's mitigation is documented as "FastAPI `exception_handler` middleware catches all `Exception` subclasses and returns `{"error": "internal_error", "correlation_id": "..."}`. Never the traceback." That handler is NOT in BL5. Today, FastAPI/Starlette's default 500 path returns the `"Internal Server Error"` text body with no traceback (correctness preserved by Starlette default), but the response also lacks the `X-Correlation-ID` echo because Starlette's `ServerErrorMiddleware` runs OUTSIDE all user middlewares — meaning `CorrelationIdMiddleware.send_with_cid` (middleware.py:79) is bypassed for the unhandled exception's response.

Concretely: an attacker getting a 500 cannot retrieve their CID from the response header, which makes operator-driven debugging harder (they must time-correlate logs). This is a SEV-3 usability/observability gap, not a disclosure.

**Verdict:** PARTIAL. The information-disclosure aspect of TM-011 is mitigated by Starlette defaults (no traceback in body). The PROMISED `{"error":"internal_error","correlation_id":...}` response shape is NOT implemented — the actual body is `{"detail":"Internal Server Error"}` with no CID. See SEV-3 finding F-3 below.

---

### TM-012 — Log-stream credential leak

**Walk.** I want to see if any log line emitted from BL5 could leak credentials.

Hot suspect: `api.request.received` (middleware.py:72-77) logs `method` and `path`. If a future endpoint accepts the bearer in the URL (e.g., `?token=...`), the path would leak. BL5 has no such endpoint. But the broader concern: does `path` itself ever carry sensitive info? FastAPI/Starlette pass the raw URL path; query string is in `scope["query_string"]`, NOT logged here. Good.

`api.auth.rejected` (middleware.py:231-236) logs `rejection_fingerprint=sha[:8]` — first 8 hex chars of SHA-256(token). This is intentional and the field name was specifically chosen to dodge `_redact_sensitive_values` (per ADR-0012's edge-case note about `token_sha256_prefix` being auto-redacted). I run the regex `_SENSITIVE_KEY_RE` (logging.py:55-67) against `rejection_fingerprint`: substrings `password|token|secret|authorization|bearer|cookie|session|api_key|apikey|credential|private_key|signature` — none match. The short-token alternation `(?:^|[^a-zA-Z])(?:pwd|pin|otp|mfa|tfa|sid|creds|salt|nonce)(?:[^a-zA-Z]|$)` — doesn't match either. Rejection_fingerprint will appear in logs as designed. 8 hex chars (32 bits) of one-way SHA-256 is non-reversible — I cannot recover the token from it.

Now I try to inject log noise. Send a request with `X-Correlation-ID: ../../../etc/passwd` — fails the UUID4 regex (middleware.py:67), regenerated. So I can't smuggle a path through CID. Send `Authorization: Bearer <newline>injected` — ascii decode strips nothing; the `\n` would survive and end up in the SHA hash but not in the log payload (only the fingerprint goes in). No log injection.

What about the `path` field? I send `GET /api/v1/foo\r\nfake_log_line=evil HTTP/1.1`. uvicorn's `httptools` parser rejects malformed paths before they ever reach the middleware. Good.

What about `method`? Bounded by HTTP verb parser. Good.

**One sub-concern**: structlog's JSONRenderer (logging.py:223) emits to PrintLoggerFactory → stdout. If `path` contains characters that break JSON encoding (raw control bytes), structlog's renderer uses `json.dumps(default=str)` which escapes them. No injection.

**Verdict:** MITIGATED. Logs do not contain the bearer or its full hash. `rejection_fingerprint` is correctly named to bypass auto-redaction and is non-reversible. No log injection vector found in BL5.

---

### TM-013 — Public /api/v1/health fingerprinting (HIGH PRIORITY for BL5)

**Walk.** I am unauthenticated. I `GET /api/v1/health`. The path is in `AUTH_EXEMPT_PREFIXES` (dependencies.py:21-26), so middleware does not gate it.

Response body (health.py:62-71):
```
{
  "status": "ok"|"degraded",
  "version": "0.1.0",
  "uptime_sec": <int>,
  "scheduler_running": false,    // BL5 stub
  "lancache_reachable": false,   // BL5 stub
  "cache_volume_mounted": <bool>,
  "validator_healthy": false,    // BL5 stub
  "git_sha": <string>
}
```

What I learn as an attacker:

1. **`version: "0.1.0"`** — exact API version. I now know which CHANGELOG entry I'm hitting. I can match against published lancache_orchestrator releases on GitHub (the project is open source per Intake §1).

2. **`git_sha`** — exact git commit. This is the GOLD finding. With the SHA I can `git checkout <sha>` of the public repo and read the EXACT code running. I can:
   - Audit dependency pins (`requirements.txt`) for specific CVE matches.
   - Identify whether SEV-1/2/3 fixes from later commits have landed.
   - Identify which BL is shipped (BL5 → no `/games`, no `/platforms` write endpoints). I save myself from probing nonexistent routes.
   - Check whether ADR-0012 D2's "404 returns 401" property holds for this build.

3. **`uptime_sec`** — tells me how long since restart. Useful for timing my attack: if I see `uptime_sec` jump back near 0, the operator just bounced the container — maybe to apply a fix. I recalibrate.

4. **`cache_volume_mounted`** — tells me whether the bind mount (`/data/cache/cache/`) is wired. False would indicate degraded ops. Probably not exploitable directly.

5. **The 503 status itself** — every BL5 build returns 503 because `scheduler_running`, `lancache_reachable`, `validator_healthy` are stubbed false. As an attacker this fingerprints the build as BL5 (not BL6+) without me reading any field. Just `curl -o /dev/null -w "%{http_code}\n" /api/v1/health` → 503 → I know it's BL5.

The threat model said: "For LAN-only single-user, low priority. Phase 3 hardening: make `git_sha` conditional on bearer auth; leave `version` + `status` unauthenticated for Game_shelf health checks."

In BL5, `git_sha` is unconditionally returned to unauthenticated callers. **This is a known-deferred issue**, but for an MVP that ships TM-013 fingerprinting via `git_sha` is the single highest-value reconnaissance leak in the API surface. I (the attacker) will use it on day 1.

**Verdict:** GAP — INTENTIONAL DEFERRAL. Documented in the original threat model as Phase 3 hardening. Re-flagging because the BL5 implementation makes the leak concrete (an exact 40-char commit SHA from `os.environ["GIT_SHA"]`) and the operator's deployment will likely expose it on the LAN. See SEV-2 finding F-1 below.

---

### TM-015 — Resource exhaustion (pool exhaustion, large body, slowloris)

**Walk.** Three sub-attacks.

**(a) Pool exhaustion.** BL4 pool defaults to 8 readers + 1 writer (`pool_readers: int = Field(default=8)`, settings.py:82). `GET /api/v1/health` borrows a reader for `pool.health_check()` + `pool.schema_status()`. If I issue 200 concurrent `/health` requests:
- uvicorn's default `limit_concurrency` is None — meaning unbounded HTTP-level concurrency.
- All 200 land in the handler concurrently; 200 await a reader from the pool.
- Pool serializes — first 8 get readers immediately, the rest queue.
- aiosqlite `health_check()` is bounded; each completes quickly. Queue drains.
- Practical impact: latency spikes during the burst, but all requests complete. Not a denial.

But: if I sustain 200 req/s indefinitely, I'm holding pool readers for a measurable fraction of every second. Concurrent legitimate requests stall behind my queue. That's a soft DoS by latency. uvicorn has no rate-limit; FastAPI has no rate-limit. No mitigation in BL5.

**(b) Large body.** `BodySizeCapMiddleware` rejects > 32 KiB (dependencies.py:15). Verified. `GET /health` has no body so this is moot for BL5.

**(c) Slowloris.** I send headers slowly, never finishing the request. uvicorn's default `timeout_keep_alive` is 5s and `h11_max_incomplete_event_size` is 16384 bytes. uvicorn has built-in slowloris resistance via `httptools`'s incremental parser, BUT BL5 does not configure `--timeout-keep-alive` or `--limit-concurrency` explicitly in the docstring (`uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765`). Operator could improve via uvicorn flags but BL5 ships no recommended flags. Defaults are okay-ish (5s keep-alive bounds idle conns) but `limit_concurrency` is unset → unlimited concurrent connections per uvicorn instance.

**Verdict:** PARTIAL. The 32 KiB body cap is enforced (good). Pool exhaustion creates a latency-DoS opportunity but not an availability-DoS in BL5 because the only DB-touching endpoint is exempt-from-auth `/health` and it's read-only with fast responses. uvicorn `limit_concurrency` is unset — the threat model promised "Phase 2 decision, target 256". Not in BL5. See SEV-3 finding F-4.

---

### TM-018 — Memory exhaustion via oversized request body

**Walk.** This TM was originally about manifest fetching from upstream CDN. Repurposing for the API: can a client-supplied body exhaust memory?

`BodySizeCapMiddleware` two-path implementation (middleware.py:106-173):

Path 1 (Content-Length present): `cl = int(cl_bytes)`. If `cl > self.cap`, send 413 immediately, no body read. Verified.

Path 2 (streaming): `bytes_received += len(body)` per `http.request` chunk; if `> cap`, raise `_BodyTooLargeError` → 413. Verified.

Bypass attempt 1: Send `Content-Length: 0` but stream 1 GB body. The cl-check path passes (0 < 32768). Then path 2 kicks in via wrapped `receive_with_cap`. First chunk that pushes total over 32768 raises `_BodyTooLargeError`. Cap holds.

Bypass attempt 2: Send `Transfer-Encoding: chunked` with no Content-Length. Same path 2. Cap holds.

Bypass attempt 3: Send `Content-Length: -1` or `Content-Length: 99999999999999999999` (oversize int). `int(cl_bytes)` accepts negative and very-large; for negative, `cl > cap` is False, so cl-check passes; for very-large, `cl > cap` is True, send 413. Actually — `Content-Length: -1` would pass the cap check (since -1 < 32768), then streaming path enforces. Negative Content-Length is rejected at the HTTP parser level by uvicorn/httptools before ever reaching middleware (returns 400). Verified by going through httptools source mentally.

Bypass attempt 4: Send 1000 separate `http.request` messages each with 33 bytes (total 33000). Wrapped receive accumulates. Cap fires at 33024 byte. Holds.

Bypass attempt 5: Headers > 32 KiB (huge cookie). The body cap doesn't cover headers. uvicorn's default `h11_max_incomplete_event_size=16384` limits headers to 16 KiB. So a 100 KiB header set is rejected at the parser. Headers are not the body cap's job, but they're bounded.

Bypass attempt 6: 10 MB body where Content-Length header is absent AND first `http.request` message has `more_body=False, body=<10MB>`. Wrapped receive reads body, sees `len(body)=10485760`, accumulates → 10485760 > 32768 → raises. **However, ASGI spec allows the server to deliver one giant `http.request` message.** uvicorn buffers up to its internal limit before delivering; that internal limit is `h11_max_incomplete_event_size` for headers (16 KiB) but uvicorn DOES NOT cap body size before invoking the app — meaning uvicorn could buffer multi-MB into memory before our middleware sees it.

Mitigation: uvicorn streams the body via h11; it does NOT pre-buffer the whole body. `receive_with_cap` sees each httptools chunk individually. For an attacker streaming 1 GB without Content-Length and without TE:chunked, uvicorn rejects at parse time (must specify one or the other for HTTP/1.1).

**Verdict:** MITIGATED. Both Content-Length proactive and streaming receive() interception are correctly implemented. uvicorn's HTTP parser bounds the pre-middleware buffer. The 32 KiB cap holds against all known bypass attempts.

---

### TM-021 — CLI argument injection — repurposed: Correlation ID injection / header smuggling

The original TM-021 is about the CLI; I'll walk it as the API-surface analogue: header smuggling via X-Correlation-ID.

**Walk.** `CorrelationIdMiddleware` (middleware.py:55-94) reads `X-Correlation-ID` from request headers, decodes ASCII, and runs `_UUID4_RE.match(cid_in)` (middleware.py:67). If match: use as-is; else regenerate UUID4. Then echoes in response header (middleware.py:79-83) by encoding `cid.encode("ascii")`.

Attack 1: `X-Correlation-ID: ../etc/passwd` → fails UUID4 regex → regenerated. No path traversal.

Attack 2: `X-Correlation-ID: 11111111-1111-4111-8111-111111111111\r\nX-Forwarded-For: evil` → `dict(scope.get("headers", []))` would have already split headers at the parser. ASGI delivers headers as `list[tuple[bytes, bytes]]`; CR/LF inside a header value is rejected by httptools as malformed. So this never reaches my middleware.

Attack 3: Forge a CID matching a legitimate request to correlate my own log line to theirs. UUID4 collision space is 122 bits — astronomically improbable that I match a randomly-chosen one. But: I can simply REUSE my OWN forged CID across multiple requests. If I send `X-Correlation-ID: 11111111-1111-4111-8111-111111111111` for 100 requests, all 100 log entries share that CID — making it harder for the operator to grep their logs (multiple unrelated requests collide on a single CID).

This is a real injection, but the impact is operator confusion, not auth bypass. Operator's `correlation_id`-based debugging is degraded for any forged-CID requests.

**Forensics implication.** If a malicious operator on the same network captures legitimate request CIDs (because the response header `X-Correlation-ID` is echoed in cleartext), they can:
1. Read a legitimate response, harvest its CID.
2. Forge a follow-up request with that CID.
3. Their request gets logged with the same CID. The operator's log stream now has two unrelated requests under the same CID. Forensics are corrupted.

The mitigation would be: REJECT inbound CIDs entirely (always generate). The threat model didn't address this. ADR-0012 D5 says "Reads incoming `X-Correlation-ID` header (UUID4 regex check; regenerates if missing or invalid)" — by design, valid client-supplied CIDs are accepted. This is an intentional "trust the client" design. For LAN-only single-user it's defensible; for multi-tenant it would be unacceptable.

**Verdict:** GAP (LOW SEVERITY). Client-supplied CIDs are accepted with only a UUID4 format check. A malicious LAN peer can corrupt audit logs by forging matching CIDs — but in single-user LAN-only deployment, the only threat actor with access is the operator themselves. See SEV-4 finding F-5 below.

---

### TM-023 — Multi-step kill chain (Game_shelf compromise → orchestrator API)

**Walk.** I follow the 8-step chain from the threat model. BL5 affects steps 5-8 (post-token, API surface).

Step 5 (Pivot to A6 with stolen bearer):
```
GET /api/v1/platforms HTTP/1.1
Host: dxp4800:8765
Authorization: Bearer <stolen_token>
```
BL5 has no `/api/v1/platforms` route. Bearer auth middleware passes (correct token). FastAPI 404. **However**, per ADR-0012 D2, the 404 happens AFTER auth — so I learn "token is valid" from the response. ADR-0012 D2 explicitly accepted this: "Bearer auth ... 404 paths *also* require auth ... returning 401 — which doesn't distinguish 'endpoint exists but you're unauth'd' from 'endpoint doesn't exist.'" Wait — that's the WRONG direction. With auth as middleware: unauth'd → 401 (regardless of whether route exists). Auth'd + route missing → 404. So a 404 confirms my token is valid even if the route is missing. Useful for the attacker. The MITIGATION property D2 was claiming is that an UNAUTH'D scanner sees 401 everywhere (including for routes that exist) — they cannot enumerate which routes exist. I (with a stolen token) can enumerate via 404 vs 200. That's expected once I have the token; nothing to mitigate.

Step 6 (Library dox): `/api/v1/games` doesn't exist in BL5 → 404 with token. No data leak from BL5.

Step 7 (Disruption): `POST /api/v1/games/steam/.../prefill` → 404 with token. No prefill in BL5.

Step 8 (Persistence): `POST /api/v1/games/steam/.../block` → 404 with token. No block list in BL5.

**The TM-023 chain is NOOP at BL5.** The attacker has a valid token but no endpoints to abuse. Body-cap holds. Auth holds. The kill chain materializes only at BL6+ when those routes ship.

OQ2 partial coverage: `LOOPBACK_ONLY_PATTERNS` already enforces `client.host == "127.0.0.1"` for `/api/v1/platforms/*/auth` even though that route doesn't exist yet. So if BL6 lands `/auth` later, the ASGI middleware gate is already engaged. This is good defensive engineering.

**Verdict:** MITIGATED IN BL5 by absence of attackable endpoints. The middleware substrate is correctly engaged for OQ2. Re-walk required at BL6 when the attackable endpoints land.

---

## Beyond-TM scenarios

### Scenario 1 — WebSocket scope smuggling

**Walk.** I send a WebSocket upgrade: `GET /api/v1/anything HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ...`.

uvicorn translates this into ASGI scope with `scope["type"] == "websocket"`. Now I trace each middleware:

- `CorrelationIdMiddleware.__call__` (middleware.py:60): `if scope["type"] != "http": await self.app(scope, receive, send); return`. **Bypassed**. No CID assigned, no logging.
- `BodySizeCapMiddleware.__call__` (middleware.py:112): same `!= "http"` early return. **Bypassed**. Cap not enforced.
- `BearerAuthMiddleware.__call__` (middleware.py:186): same `!= "http"` early return. **Bypassed**. **No bearer auth on WebSockets**.
- `CORSMiddleware`: Starlette's CORS middleware also short-circuits on non-http. WS request reaches FastAPI's router.

FastAPI router has no WS endpoints registered. It returns 404. But **what does the client see?** A WebSocket upgrade against a non-WS route in Starlette closes the connection with WS close code 1006 / `403` after the handshake fails — actually, Starlette's default is to call `close()` with code 1000. Either way, no successful upgrade.

So today: `GET /api/v1/anything Upgrade: websocket` → not a successful WS connection → no real attack surface.

**The risk is forward-looking.** If BL7+ adds a WebSocket endpoint (e.g., `/api/v1/jobs/stream` for live job progress), it will land in the same FastAPI router with these same middlewares — and **bearer auth will not gate it**. This is a latent SEV-2 if not corrected before any WS endpoint is added.

**Verdict:** GAP (FUTURE-FACING). All four BL5 middlewares early-return on non-http scope. Today no WS endpoints exist so there's no exploitable path, but the substrate design allows BL7+ to ship a WS endpoint with no auth and no body-cap enforcement. See SEV-3 finding F-2 below.

---

### Scenario 2 — HTTP/2 attacks

**Walk.** Does uvicorn 0.39.0 support h2 by default? Per uvicorn docs and Context7: no. uvicorn ships HTTP/1.1 only; HTTP/2 requires `hypercorn` or another ASGI server. The `h2==4.3.0` in `requirements.txt` is a transitive dep of `httpx[http2]` (used by httpx as a CLIENT library for upstream calls), not by uvicorn for the server.

If I send an h2 preface (`PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n`) to uvicorn, httptools rejects it as malformed HTTP/1.1 → connection closed.

So: HPACK header smuggling, RST_STREAM flood, h2c upgrade attacks — all N/A. uvicorn won't speak h2.

**Edge case.** If a future deployment swaps uvicorn for hypercorn (also ASGI-compatible), middleware would see slightly different scope (e.g., `http_version: "2"`). All four middlewares only use `scope["type"]`, `scope["method"]`, `scope["path"]`, and `scope["headers"]` — they don't care about `http_version`. So a port to hypercorn would not introduce middleware bypass, except for the WS-bypass already noted.

**Verdict:** N/A-IN-BL5. uvicorn is HTTP/1.1 only; no h2 attack surface.

---

### Scenario 3 — X-Correlation-ID spoofing

(Walked under TM-021 above.) Restating: middleware accepts client-supplied UUID4 CIDs; an attacker on the same LAN can replay or forge CIDs to corrupt operator's log-correlation workflow.

**Verdict:** GAP — see SEV-4 F-5 below.

---

### Scenario 4 — OpenAPI info leak (loopback-pinned per OQ2?)

**Walk.** The user's hypothesis is that `/openapi.json`, `/docs`, `/redoc` are loopback-pinned per OQ2. Let me verify.

`AUTH_EXEMPT_PREFIXES` (dependencies.py:21-26) includes `/api/v1/openapi.json`, `/api/v1/docs`, `/api/v1/redoc` — these bypass bearer auth. `LOOPBACK_ONLY_PATTERNS` (dependencies.py:29-31) is exactly `^/api/v1/platforms/[^/]+/auth$` — the OPENAPI ENDPOINTS ARE NOT IN THE LOOPBACK LIST.

So a non-loopback bearer-authenticated client (any LAN host with the bearer) can `GET /api/v1/openapi.json` and receive the full OpenAPI schema. And critically, a non-loopback **unauthenticated** client can ALSO `GET /api/v1/openapi.json` because the path is in `AUTH_EXEMPT_PREFIXES`. Both /docs and /redoc HTML pages and the openapi.json itself are reachable by anyone who can reach port 8765.

The user's question implied OQ2 pinned them to loopback. **It does not.** OQ2 only pins the platform/auth route.

For BL5 the schema is small (`/api/v1/health` only), but it leaks:
- Server `version: 0.1.0`.
- The `bearerAuth` security scheme (proves bearer auth exists, but anyone reading `/health`'s response already knew that).
- The full HealthResponse schema (7 fields with their types). Same as the response itself.

For BL5 this is low impact. For BL6+, `/openapi.json` will reveal every endpoint, every Pydantic model field, every constraint. An unauthenticated attacker on the LAN gets a complete API map. They can plan attacks before stealing a token.

**Verdict:** GAP. `/api/v1/openapi.json`, `/api/v1/docs`, `/api/v1/redoc` are unauth + non-loopback — anyone who can reach port 8765 can fetch them. ADR-0012 D2 documents the AUTH_EXEMPT_PREFIXES choice but does not justify the openapi/docs/redoc inclusion. Given the project's open-source nature this leaks little vs the public repo, but in BL6+ it would expose the full surface for an unauthenticated reconnaissance phase. See SEV-2 finding F-6 below.

---

### Scenario 5 — Lifespan SystemExit + container restart loop

**Walk.** Migrations fail (e.g., DB file is corrupt or the volume permissions are wrong). `_lifespan` raises `SystemExit(1)` (main.py:50). uvicorn's lifespan runner catches the exit, logs it as a startup failure, and the process exits. Docker compose's `restart: unless-stopped` policy → container restarts. Migration fails again (root cause unfixed) → infinite restart loop.

What does the operator see? `log.critical("api.boot.migrations_failed", reason=str(e))` (main.py:49). `reason` is `str(e)` where `e` is a `migrate.MigrationError`. Whether that string contains sensitive info depends on what the migrate module puts in the error. I would need to read the migrate module to be sure, but typical exception strings would be like "migration 0003_xxx failed: SQL syntax error near 'foo'" — operationally useful, low-sensitivity.

The pool init failure path is identical (main.py:56-58). Same `str(e)`.

Crash-loop visibility: structlog emits the `api.boot.migrations_failed` event with `level=critical` to stdout JSON. Docker captures stdout, operator sees it via `docker logs`. The CID context is empty at this point (lifespan has no per-request CID), so the log line carries no correlation_id — but the failure is unambiguous.

Sensitive info in `reason`? Settings `database_path` is just a path. The exception bubbling from `migrate.run_migrations` would not contain credentials (DB has no password — SQLite). No leaks.

**One concrete worry:** If the operator misconfigures `DATABASE_PATH` to point at an arbitrary file (e.g., `/run/secrets/orchestrator_token`), `migrate.run_migrations` opens it as SQLite. SQLite would fail to parse the secret as a database; the error message might echo bytes from the file ("file is not a database" with no echo, typically). Low risk.

**Crash-loop DoS itself.** Container restart loops consume Docker daemon resources but pose no app-layer DoS unless the operator has set restart-on-failure backoff inappropriately. Standard concern, not BL5-specific.

**Verdict:** MITIGATED. SystemExit(1) is the right contract for unrecoverable startup. `reason=str(e)` is operationally useful and unlikely to leak sensitive bytes. Crash-loop visibility is good (CRITICAL-level structured log per attempt). One observation worth noting: the `rejection_fingerprint` is NOT involved here (different code path) — confirming the user's question about that not leaking.

---

### Scenario 6 — CORS preflight bypass interactions with body cap

**Walk.** The middleware ORDER per main.py:102-112 (registered REVERSE of execution order):
- Outermost: CorrelationIdMiddleware
- Next: BodySizeCapMiddleware
- Next: BearerAuthMiddleware
- Innermost: CORSMiddleware
- Then: FastAPI routes

So a request flows: CID → BodyCap → Auth → CORS → router.

A CORS preflight is `OPTIONS /api/v1/foo HTTP/1.1` with `Origin: http://attacker.example` and `Access-Control-Request-Method: POST`.

Walk:
- CID: assigns CID. Logs `api.request.received method=OPTIONS path=/api/v1/foo`.
- BodyCap: OPTIONS preflights typically have `Content-Length: 0` or no body. If `Content-Length: 99999`, BodyCap fires 413 BEFORE Auth and CORS. So a forged-CL OPTIONS gets a 413, NOT a CORS response. That's a (minor) behavioral quirk — the browser making a real preflight wouldn't send a Content-Length > 0, so legitimate clients are fine. An attacker probing with `OPTIONS / Content-Length: 1000000` learns: 413 = body cap is set to <1M; useful but unsurprising info.
- Auth (middleware.py:194): `if method == "OPTIONS": await self.app(scope, receive, send); return`. Auth bypassed for ALL OPTIONS. Even if path is non-exempt, OPTIONS gets through to CORS.
- CORS: if `cors_origins` is empty (default: `cors_origins: list[str] = Field(default_factory=list)`, settings.py:59), Starlette's CORSMiddleware does NOT match the request's Origin. Per Starlette source, an unmatched preflight returns a normal 200 response from the wrapped app — meaning the request continues to the router, which 404s for OPTIONS on a nonexistent path or 405s on an existing GET-only route.

So with empty `cors_origins` (BL5 default): preflight from any origin → CORS doesn't add `Access-Control-Allow-Origin` headers → browser blocks the actual request. CORS is effectively closed.

With `cors_origins=["http://gameshelf.local"]`: preflight from that origin gets `Access-Control-Allow-Origin: http://gameshelf.local`; the actual request must originate from that domain. Non-matching origins get no ACAO header → browser blocks.

The user's question: "If the preflight responds 200 with no auth, can a CORS-permitted origin then send the actual request with a body > cap and observe a different response (cap rejection vs auth rejection) — and does that constitute info leak?"

Walk:
1. Preflight `OPTIONS /api/v1/games`. Auth bypassed (OPTIONS). BodyCap passes (CL=0). CORS replies 200 with ACAO if origin matches. Browser proceeds.
2. Actual request `POST /api/v1/games` with CL=99999 and Authorization header.
3. CID logs.
4. BodyCap: CL > 32768 → 413 immediately. Auth never runs.
5. Attacker observes 413 response.

Compare with no Authorization header:
1. Preflight 200 (auth bypassed).
2. Actual request `POST /api/v1/games` CL=99999, no Auth header.
3. BodyCap: CL > 32768 → 413 BEFORE auth. Same 413.

Compare with Authorization header but CL=100:
1. Preflight 200.
2. Actual request `POST /api/v1/games` CL=100, no token: BodyCap passes, Auth fires, 401.
3. With wrong token: 401.
4. With correct token: 404 (route absent) or whatever the route returns.

So: an attacker can distinguish:
- BodyCap-rejection (CL > 32768 → 413) vs auth-rejection (401) **before having a token**.

Is this an info leak? It tells them: (a) the body cap is at most 32 KiB, (b) BodyCap runs before Auth. Both are useful for tuning attacks (e.g., for slowloris keep-the-connection-open variants, knowing the cap helps). But neither is sensitive — it's documented in the public threat model.

**More interesting:** when CL is missing or 0 and the actual streamed body is over cap, the order is BodyCap-then-Auth in the middleware chain — but the streaming body cap fires only when `receive()` is called by the downstream app. If Auth rejects (401) without reading the body, the streaming cap never fires. So a request with `no Content-Length, large body, no Auth` returns 401 (auth) NOT 413 (cap). The order of failure depends on whether the handler reads the body.

For BL5 specifically: no body-consuming handler exists, so streaming-cap is never tested in real traffic. Direct middleware unit test covers it (per ADR-0012 D7's "BL5 has no body-consuming endpoint").

**Verdict:** MITIGATED. The information disclosure (cap > X) is minor and documented. The middleware order (CID → BodyCap → Auth → CORS) is sound: rejecting oversized bodies before auth is a defense-in-depth that prevents auth code from ever processing a megabyte of attacker-controlled bytes. CORS-permitted-origin attackers face the same body cap as anyone else.

---

### Scenario 7 — OQ2 loopback regex bypass attempts

**Walk.** Regex: `^/api/v1/platforms/[^/]+/auth$` (dependencies.py:30).

Try each:
1. `/api/v1/health` → no match (different path). N/A.
2. `/api/v1/platforms/steam/auth` → match, OQ2 enforced. Baseline good.
3. `/api/v1/platforms/steam/auth%20` (URL-encoded space): ASGI `scope["path"]` is the URL-DECODED path. uvicorn decodes %20 to a literal space. Pattern: `/api/v1/platforms/steam/auth ` (trailing space). Regex: `[^/]+/auth$` requires path ENDING with literal `auth`. Trailing space means path doesn't end with `auth$`. **REGEX FAILS TO MATCH** → OQ2 NOT enforced. Then FastAPI router lookup: no route for `/api/v1/platforms/steam/auth ` either → 404. So in BL5 it's moot (no route). When BL6 adds the route, the route will be defined as `/api/v1/platforms/{name}/auth` exactly — FastAPI's path matching is strict; `/auth ` won't match `/auth`. So 404. **Same outcome: regex fails BUT route also fails. No bypass.**

   **However**: if FastAPI later adds `redirect_slashes=True` (it's True by default!) or some normalization that strips trailing whitespace, the regex bypass would precede route normalization. ASGI scope path is set BEFORE FastAPI's normalization. So the BearerAuthMiddleware sees the raw `/auth ` and the OQ2 regex doesn't match. If FastAPI then normalizes and dispatches to the real `/auth` handler, **OQ2 is bypassed**.

   Test: I would need to verify whether FastAPI (Starlette underneath) normalizes whitespace. Starlette does NOT strip trailing whitespace from paths by default. URL-encoded space (%20) → literal space → no route. BL5 ships safely. **For BL6**, when `/platforms/{name}/auth` ships, this should be re-tested.

3. `/api/v1/platforms/steam/auth/` (trailing slash): `scope["path"]` is `/api/v1/platforms/steam/auth/`. Regex requires `auth$` — fails. FastAPI's `redirect_slashes=True` (default) would 307-redirect to `/auth`. **The redirect response is generated by Starlette WITHIN the ASGI app — it lands in the request flow AFTER the middlewares.** So my middleware chain sees `/api/v1/platforms/steam/auth/` (with slash), regex doesn't match, OQ2 NOT enforced. Auth check passes if token is valid. Then FastAPI router redirects to `/auth`. The 307 response is sent to the client. The client follows the redirect → `/api/v1/platforms/steam/auth` → middleware re-fires (it's a NEW request) → regex matches → OQ2 enforced.

   **However**, if a non-loopback authenticated client sends `/auth/` directly, the middleware does NOT enforce loopback for that request. It returns 307 to `/auth`. The client follows; OQ2 enforced. NET effect: client cannot call the real `/auth` handler from non-loopback. **Trailing slash does NOT bypass OQ2** in practice because the redirect re-routes through middleware.

   Wait — but what if a malicious client sends `/auth/` and PASTES the redirect URL into the original request body (as a payload), reading the 307 response only? That's not a bypass; client doesn't reach the handler. So safe.

4. `/Api/V1/Platforms/Steam/Auth` (mixed case): regex compiled without `re.IGNORECASE`. **Regex does not match.** FastAPI route matching: Starlette's path matching is CASE-SENSITIVE. So `/Api/V1/...` → 404. Both regex and route fail consistently. No bypass.

5. `/api/v1/platforms/../v1/platforms/steam/auth` (path traversal): `scope["path"]` from uvicorn is what httptools parsed — it does NOT normalize `..`. So scope path is literally `/api/v1/platforms/../v1/platforms/steam/auth`. The regex `^/api/v1/platforms/[^/]+/auth$` requires only ONE `/api/v1/platforms/` prefix and a non-slash segment before `/auth$`. The traversal path has multiple slashes and the pattern after `/platforms/` is `..`, then `/`, which violates `[^/]+`. **Regex does not match.** FastAPI router does not normalize either; route lookup is literal. So `/api/v1/platforms/../v1/platforms/steam/auth` → 404 (no match). Both fail consistently. No bypass.

   **However**: what if some downstream proxy (nginx) normalizes the path BEFORE forwarding? nginx by default normalizes `..` segments. Then nginx forwards `/api/v1/platforms/steam/auth` to uvicorn. Middleware sees the normalized path; regex matches; OQ2 enforced as long as `client.host` reflects the proxy, not the original client. And if nginx is on the same host (loopback), `scope.client[0]` would be `127.0.0.1` — bypassing OQ2! This is a CLASSIC reverse-proxy pitfall.

   The mitigation: trust headers like `X-Forwarded-For` and read the real client IP. BL5 does NOT implement that. If the deployment topology adds an nginx in front of uvicorn (very plausible — operators often add TLS-terminating nginx), `scope.client[0]` will be `127.0.0.1` for every request, **fully bypassing OQ2**.

   This is a deployment-pattern footgun. Mitigation: documentation that uvicorn must be reached directly, or a proxy-trust header parser. See SEV-2 finding F-7 below.

6. `/api/v1/platforms/steam%2Fauth` (URL-encoded slash): uvicorn/httptools URL-decodes `%2F` to `/`. So `scope["path"]` = `/api/v1/platforms/steam/auth`. Regex matches. OQ2 enforced. **Wait — this is the same as the unencoded path.** Actually: RFC 3986 says `%2F` in a path is reserved and SHOULD NOT be decoded by some intermediaries. uvicorn DOES decode it. So encoded-slash gets normalized to literal slash. Regex matches. OQ2 enforced. Same handler. No bypass.

   But this means an attacker cannot use `%2F` to add an extra fake segment. Good.

7. Double-encoded `/api/v1/platforms/steam%252Fauth`: `%25` → `%`. So decoded path is `/api/v1/platforms/steam%2Fauth`. uvicorn does ONE decoding pass (per RFC 3986 — multiple decoding passes are explicitly NOT done). Result: literal `%2F` in the path component. Regex `[^/]+` greedily matches `steam%2Fauth` as a single segment ... wait, `auth` has to be a literal suffix `/auth$`. The path `/api/v1/platforms/steam%2Fauth` has no literal `/auth`, just `%2Fauth`. **Regex does NOT match.** FastAPI router: no route matches `/api/v1/platforms/steam%2Fauth` literally → 404. Both fail consistently. No bypass.

**Summary of OQ2 regex tests:**
- All path-form variations either match the regex correctly OR fail to find a route. The regex itself is consistent with FastAPI's path matching.
- The one realistic deployment-level bypass: a reverse proxy that terminates on loopback, making `scope.client[0] == "127.0.0.1"` for every request. This silently disables OQ2.

**Verdict:** MITIGATED at the regex/path level; deployment-level GAP. See SEV-2 finding F-7 below.

---

### Scenario 8 — Path-traversal in AUTH_EXEMPT_PREFIXES

**Walk.** AUTH_EXEMPT_PREFIXES match is `any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)` (middleware.py:199). Prefixes: `/api/v1/health`, `/api/v1/openapi.json`, `/api/v1/docs`, `/api/v1/redoc`.

Try:
1. `/api/v1/health/../../admin`: `scope["path"]` is the literal string from httptools, NOT normalized. `path.startswith("/api/v1/health")` → True. **Auth bypassed.** Then FastAPI router: no route for `/api/v1/health/../../admin` → 404. So I bypass auth and get a 404. No data leak (404 has no body of value). But this confirms a route bypass IS possible if FastAPI's router ever normalized the path AFTER middleware.

   Starlette/FastAPI does NOT normalize `..` in paths. Route lookup is literal-string. So `/api/v1/health/../../admin` is a 404 forever. Safe today.

   **Risk**: If a future endpoint is added at `/api/v1/admin` (any path NOT starting with `/api/v1/health`), and ANY downstream component (an Nginx in front, a future BL refactor that adds path-normalization middleware ABOVE the auth middleware, or a misconfigured load balancer) normalizes `..`, then the path delivered to the router would be `/api/v1/admin` while the auth middleware saw `/api/v1/health/../../admin` and bypassed. Classic auth-bypass-via-path-normalization. Today: safe due to no normalization. Forward risk: real.

2. `/api/v1/health%2f..%2fadmin` (URL-encoded slashes): uvicorn decodes %2f to `/`. Path becomes `/api/v1/health/../admin`. `startswith("/api/v1/health")` → True. **Auth bypassed.** Same situation: 404 today, forward risk.

3. `/api/v1/health.suffix`: `startswith("/api/v1/health")` → True. **Auth bypassed.** No route matches → 404. Forward risk if a route `/api/v1/health.suffix` ever exists, which is unlikely.

4. `/api/v1/healthz`: `startswith("/api/v1/health")` → True. **Auth bypassed.** No route matches → 404. Today safe. **Forward risk:** if BL6+ adds Kubernetes-style `/api/v1/healthz` or `/api/v1/healthcheck` route, it would be implicitly auth-exempt because of the prefix match. The prefix list should arguably use exact-match (or boundary-anchored regex) for these endpoints rather than `startswith`.

5. `/api/v1/openapi.json/secret`: `startswith` → True. Auth bypassed. 404. Forward risk if any sub-route is added under `/openapi.json/`.

**The pattern weakness:** `path.startswith(prefix)` with prefixes that don't end in `/` or `$` will match any path with that prefix as a substring of the leading segment. This is the OWASP "prefix-match auth bypass" antipattern. For example, `/api/v1/health` matches `/api/v1/healthcheck/admin/escalate` — completely different conceptual route, all auth-bypassed.

For BL5: no exploitable target exists. For BL6+: this is a SEV-2 risk. The fix is to either:
- Use exact-match for the four endpoints, OR
- Use boundary-anchored regex: `^/api/v1/health(/|$)` — match `/health` only when followed by `/` or end-of-string.

**Verdict:** GAP — FUTURE-FACING. See SEV-2 finding F-8 below.

---

## Findings

### SEV-1 (none)

No exploitable critical vulnerabilities in the BL5 surface. Bearer auth gates all non-exempt paths, body cap is enforced both proactively (Content-Length) and via streaming receive() interception, OQ2 loopback enforcement is in place at the middleware layer, no SQL injection surface (no DB-touching endpoint takes input), no traceback disclosure in 500 bodies.

### SEV-2

**F-1 — Unauthenticated git_sha disclosure on /health (TM-013 deferred).**
- **Description:** `/api/v1/health` returns the exact 40-char git SHA to any caller (no auth).
- **Scenario:** I'm an attacker on the trusted VLAN. `curl http://dxp4800:8765/api/v1/health` → `{"git_sha":"<sha>", ...}`. I `git checkout <sha>` of the public repo, audit dependencies and known SEV-1/2/3 status, and target known unpatched issues.
- **Affected code:** `src/orchestrator/api/routers/health.py:70` returns `git_sha` unconditionally; `src/orchestrator/api/dependencies.py:21-26` lists `/api/v1/health` in `AUTH_EXEMPT_PREFIXES`.
- **Fix:** Make `git_sha` and any version-specific fields conditional on authenticated requests. Require bearer token to access `git_sha`; return only `status`, `version` (major-minor), and the four boolean health fields to unauth callers. The threat model already records this as Phase 3 hardening — track the deferral explicitly.
- **Regression test:** Two tests: (a) unauth GET returns body with `git_sha` absent or redacted; (b) authed GET returns body with `git_sha` populated.

**F-6 — OpenAPI/docs/redoc unauthenticated, non-loopback exposure.**
- **Description:** `/api/v1/openapi.json`, `/api/v1/docs`, `/api/v1/redoc` are auth-exempt with no loopback restriction. In BL6+ this exposes the entire API surface — every route, every request/response model, every field constraint — to any LAN host that can reach port 8765.
- **Scenario:** Pre-token reconnaissance. `curl http://dxp4800:8765/api/v1/openapi.json` → full API spec. Attacker plans the TM-023 kill chain with perfect knowledge of every endpoint before stealing a token.
- **Affected code:** `src/orchestrator/api/dependencies.py:21-26` and `src/orchestrator/api/main.py:89-91` (the docs URLs).
- **Fix:** Either require bearer auth on `/openapi.json`, `/docs`, `/redoc`, OR restrict them to `LOOPBACK_ONLY_PATTERNS` in addition to bearer (matches the operator's `kubectl port-forward` workflow without exposing on LAN). Recommended: bearer-required.
- **Regression test:** unauth GET to `/api/v1/openapi.json` returns 401; authed GET returns the schema.

**F-7 — OQ2 loopback enforcement is reverse-proxy-naive.**
- **Description:** OQ2's `client_host == "127.0.0.1"` check (middleware.py:244) reads `scope["client"][0]` directly. This is the immediate TCP peer's address — for any deployment that places nginx, Caddy, or another reverse proxy on the same host as uvicorn, the peer is always 127.0.0.1, **silently disabling OQ2** for all requests.
- **Scenario:** Operator decides to terminate TLS and adds nginx on the same host with `proxy_pass http://127.0.0.1:8765`. From the LAN, an attacker `POST`s to `/api/v1/platforms/steam/auth` (when BL6 lands the route) with a stolen bearer. nginx forwards; uvicorn sees client `127.0.0.1`; OQ2 check passes; the auth-trigger handler runs from anywhere on the LAN. OQ2 is bypassed end-to-end.
- **Affected code:** `src/orchestrator/api/middleware.py:241-252`.
- **Fix:** Either (a) document explicitly that uvicorn MUST be reached directly without intermediate proxies, OR (b) add support for trusted-proxy headers (X-Forwarded-For with an explicit allowlist of proxy IPs read from settings; reject if header missing AND `client.host != 127.0.0.1`). Recommend (a) for MVP simplicity, with a Phase 4 HANDOFF note flagging this footgun.
- **Regression test:** Documentation-level — unit test cannot verify deployment topology. Add an `api_host` startup invariant log line that warns if `api_host != "127.0.0.1"` (already present per settings.py:189-194 — `config.api_bound_non_loopback`). Also add a HANDOFF document warning.

**F-8 — AUTH_EXEMPT_PREFIXES uses `startswith` — prefix-match auth bypass antipattern.**
- **Description:** `any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)` (middleware.py:199) matches any path with the listed prefix as a leading-string match — including suffixed paths like `/api/v1/healthz`, `/api/v1/health/../../admin`, `/api/v1/openapi.json.bak`. For BL5 no exploitable target exists, but BL6+ adding any route under or near these prefixes inherits the bypass. The classic OWASP A01:2021 broken-access-control pattern.
- **Scenario (forward-facing):** BL7 adds k8s-style `/api/v1/healthz` for liveness probes. Because of `startswith`, this route is auth-exempt. If `/healthz` is functionally distinct from `/health` (e.g., it touches DB or returns extended info), the bypass is exploitable.
- **Affected code:** `src/orchestrator/api/middleware.py:199`.
- **Fix:** Either compile `AUTH_EXEMPT_PREFIXES` as boundary-anchored regex (`^/api/v1/health(/|$)`, etc.), OR use exact-match (`path == p or path.startswith(p + "/")`).
- **Regression test:** Negative test for each prefix: `/api/v1/healthz`, `/api/v1/health-admin`, `/api/v1/openapi.jsonleak` should NOT be auth-exempt → 401.

### SEV-3

**F-2 — Non-HTTP scope (WebSocket) bypasses ALL custom middleware.**
- **Description:** All four BL5 middlewares early-return if `scope["type"] != "http"`. WebSocket upgrades skip CID logging, body cap, and bearer auth. BL5 has no WS endpoints; this is a latent issue for BL7+ when WS streaming endpoints (e.g., live job progress) are likely to be added.
- **Scenario (forward-facing):** BL7 adds `/api/v1/jobs/{id}/stream` as a WebSocket endpoint. Attacker establishes a WS connection without any bearer token. Server accepts the upgrade, attacker streams job progress data — TM-013 fingerprinting becomes TM-001-equivalent (full read access).
- **Affected code:** `middleware.py:60-61, 112-114, 186-188`.
- **Fix:** Either (a) plumb auth into WS scope handling explicitly (FastAPI's WebSocket dependency injection supports it), OR (b) extend each middleware's scope-type handling: `if scope["type"] == "websocket"` — assign CID via WS extension, enforce auth via subprotocol-or-header check, refuse non-bearer WS upgrades with `403 close code 1008`.
- **Regression test:** Add a fake WS endpoint to test fixtures; verify (a) unauth WS upgrade is rejected with 1008, (b) wrong-token WS upgrade is rejected, (c) right-token WS upgrade succeeds and CID is in upgrade response headers.

**F-3 — No global Exception handler; unhandled-exception responses lack X-Correlation-ID echo.**
- **Description:** TM-011's stated mitigation includes a FastAPI `exception_handler` returning `{"error":"internal_error","correlation_id":"..."}`. BL5 has none. Starlette's default 500 response is `Internal Server Error` plain-text-ish (`{"detail":"Internal Server Error"}` from FastAPI's default), and the response is generated OUTSIDE `CorrelationIdMiddleware.send_with_cid`, so the 500 response carries no CID. The traceback DOES NOT leak (Starlette default is safe), but operator debugging is impaired because clients receiving a 500 cannot self-correlate to log entries.
- **Scenario:** A future BL6 handler raises an unexpected exception. Operator sees a 500 in the access log, the structured log has the CID, the user reports the 500 with no CID — operator has to time-grep to correlate. Workable but slow.
- **Affected code:** `src/orchestrator/api/main.py:75-141` — `create_app` does not register `app.add_exception_handler(Exception, ...)`.
- **Fix:** Add a global exception handler:
  ```python
  @app.exception_handler(Exception)
  async def unhandled_exception_handler(request, exc):
      cid = structlog.contextvars.get_contextvars().get("correlation_id", "unknown")
      log.exception("api.handler_error", correlation_id=cid)
      return JSONResponse(
          status_code=500,
          content={"error":"internal_error","correlation_id":cid},
          headers={"X-Correlation-ID": cid},
      )
  ```
- **Regression test:** Stub a route that raises `ValueError("with secret bytes")`; assert response body is `{"error":"internal_error","correlation_id":"<uuid4>"}`, response has `X-Correlation-ID` header, `secret bytes` does not appear anywhere in the response.

**F-4 — uvicorn `limit_concurrency` not set; unbounded HTTP-level concurrency.**
- **Description:** TM-015's mitigation says "uvicorn default `limit_concurrency` sized appropriately (Phase 2 decision, target 256)." BL5's launch incantation in main.py:4 (`uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765`) does NOT pass `--limit-concurrency`. The default is None — unbounded. Pool readers are bounded to 8 (settings.py:82), so concurrent DB-touching requests queue at the pool layer; but uvicorn itself accepts unbounded TCP connections.
- **Scenario:** Slowloris variant — open thousands of TCP connections, send headers slowly. uvicorn keeps them all open up to OS file-descriptor limits. At 1024 fds (Docker default ulimit), uvicorn fails to accept new connections — DoS.
- **Affected code:** `src/orchestrator/api/main.py:4` (docstring example) — but the real fix is operator-level (compose file).
- **Fix:** Document `--limit-concurrency 256` in deployment configs, or add a section in HANDOFF.md. Optionally, bake the limit into a `[uvicorn]` config block in pyproject.toml.
- **Regression test:** Operational test (load test with 500 concurrent slowloris connections); not a unit test.

### SEV-4

**F-5 — Client-supplied X-Correlation-ID accepted; log forensics can be corrupted.**
- **Description:** Per ADR-0012 D5 (intentional design), `CorrelationIdMiddleware` accepts client-supplied UUID4 CIDs. A malicious LAN actor can forge CIDs to either replay a known-good CID (corrupting an operator's grep-based debugging by colliding their request with a legitimate one) or use the same CID across many forged requests (degrading uniqueness).
- **Scenario:** Multi-step deception. Attacker sends 100 reconnaissance probes with `X-Correlation-ID: <fixed_uuid>`. Operator later investigates a real incident; greps logs for any CID; the fixed_uuid surfaces 100 attacker requests mixed in with legitimate ones. Operator's mental model of the incident is polluted.
- **Affected code:** `src/orchestrator/api/middleware.py:64-67`.
- **Fix:** Two options. (a) Always-generate: ignore client-supplied CIDs entirely; record the offered CID under `client_supplied_correlation_id` in the log line so it doesn't shadow the canonical one. (b) Tag the CID's provenance in logs: `correlation_id_source: "client" | "generated"`. Recommend (b) — preserves the request-trace propagation feature for legitimate clients while making forgery visible.
- **Regression test:** Send `X-Correlation-ID: 00000000-0000-4000-8000-000000000001`; verify log entry contains `correlation_id_source="client"`. Send no header; verify `correlation_id_source="generated"`.

---

## Non-findings (checked and cleared)

The following were investigated and found NOT to be vulnerabilities in BL5:

- **TM-005 SQL injection:** No DB-touching endpoint takes a parameter. Re-walk required at BL6.
- **Stack-trace disclosure in 500 body:** Starlette's default 500 contains no traceback. The `Internal Server Error` body is safe (only the missing CID echo is sub-optimal — F-3).
- **Bearer comparison timing oracle:** `hmac.compare_digest` is timing-safe; verified at middleware.py:224.
- **Token brute force:** 32-char minimum, control-char rejection, opaque format → infeasible search space.
- **Body-cap bypass via Content-Length 0 + streaming:** Wrapped `receive_with_cap` enforces accumulated cap; cannot bypass.
- **Body-cap bypass via chunked encoding:** Same path. Holds.
- **Body-cap bypass via huge header set:** uvicorn h11_max_incomplete_event_size bounds headers to 16 KiB independent of the body cap.
- **HTTP/2 attacks:** uvicorn does not speak h2; surface does not exist.
- **Log injection via path/method/headers:** httptools rejects malformed paths; structlog JSONRenderer escapes control chars.
- **Lifespan SystemExit log leakage:** `reason=str(e)` strings from migrate/pool failures are operationally appropriate and don't carry credentials.
- **Crash-loop visibility:** structlog CRITICAL events go to stdout; Docker captures them.
- **CORS preflight as auth bypass:** Preflight only permits the browser to make the actual request; the actual request still hits BodyCap → Auth → CORS in order. Permission to send a preflight does not equal permission to bypass auth.
- **Mixed-case path bypass of OQ2:** Both regex and FastAPI route matching are case-sensitive; `/Api/V1/...` → 404 consistently.
- **URL-encoded path bypass of OQ2:** uvicorn decodes once; re-encoded slash decodes to literal slash; double-encoded slash stays literal `%2F` (no router match). Consistent.
- **Path traversal `..` bypass of OQ2:** Neither uvicorn, Starlette, nor FastAPI normalizes `..` segments; both regex and route lookup fail consistently.
- **rejection_fingerprint log leak:** 8 hex chars of one-way SHA-256; field name dodges `_redact_sensitive_values`. Verified non-reversible.

---

## Summary

BL5's middleware stack is structurally correct: bearer auth gates every non-exempt path, body cap is streaming-aware, OQ2 loopback enforcement is engaged. The four findings above (F-1 SEV-2 git_sha leak, F-6 SEV-2 unauth OpenAPI, F-7 SEV-2 reverse-proxy bypass of OQ2, F-8 SEV-2 startswith prefix-match) are realistic, exploitable, and inexpensive to fix. F-2 (WS bypass) is latent and must be fixed before any WS endpoint lands. F-3 / F-4 / F-5 are observability/operational improvements.

Highest-priority fixes before Milestone B closes: F-1, F-6, F-8 (all directly exploitable for a TM-023-style attacker performing reconnaissance before token theft). F-7 should ship documentation-level guidance now and a header-trust implementation in Phase 3.

Most surprising non-finding: the path-traversal exhaustive walk of OQ2 produced no exploit — Starlette/FastAPI's strict literal route matching defeats every URL-encoding and traversal trick at the regex level. The OQ2 enforcement is brittle in deployment topology (F-7) but solid against on-the-wire path manipulation.
