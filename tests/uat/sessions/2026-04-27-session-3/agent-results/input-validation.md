# UAT-3 Input Validation Fuzz
**Agent:** input-validation
**Date:** 2026-04-27
**Scope:** BL5 — `src/orchestrator/api/{main,dependencies,middleware,routers/health}.py`
**Persona:** QA test engineer + malicious user (combined)

This is a code-trace audit (not runtime fuzzing). Every "Actual" cell below is derived from reading the source. Where the contract is unclear or the code is fragile, a regression test sketch is provided in the Findings section.

Reference excerpts that are repeatedly cited:
- `_UUID4_RE` matches a strict v4 UUID (case-insensitive), anchored `^…$`.
- Correlation ID parse: `cid_in = cid_bytes.decode("ascii", errors="ignore")` then `_UUID4_RE.match(cid_in) else uuid4()`.
- Auth parse: `headers.get(b"authorization", b"").decode("ascii", errors="ignore")`; require literal prefix `"Bearer "`; strip token; `hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))`.
- Body cap (CL path): `int(cl_bytes); except ValueError: cl = 0`. Streaming path increments `bytes_received` per `http.request` chunk and raises `_BodyTooLargeError` if `> cap`.
- Auth-exempt match: `path.startswith(p) for p in AUTH_EXEMPT_PREFIXES`. No normalization.
- Loopback check: `client_info = scope.get("client"); client_host = client_info[0] if client_info else None; if client_host != "127.0.0.1": 403`.

---

## Vector A: X-Correlation-ID

| Input | Expected | Actual (per code) | Gap? |
|---|---|---|---|
| missing | regenerate v4 | `cid_bytes=b""` → `cid_in=""` → no UUID4 match → fresh `uuid4()` | OK |
| `""` (empty header) | regenerate | empty decode → no match → regenerate | OK |
| `" "` whitespace | regenerate | no match → regenerate | OK |
| Embedded NUL `"abc\x00def"` | regenerate | header bytes pass ASCII decode (NUL is ASCII), then regex fails → regenerate. Note: ASGI servers (h11, hypercorn, uvicorn-httptools) typically reject NUL in headers at the parser layer before it reaches middleware. | OK at app layer; relies on transport layer, but defensive regex catches it anyway |
| CRLF injection `"abc\r\nX-Injected: pwned"` | regenerate (or reject upstream) | `\r\n` is ASCII, decode succeeds, regex fails → regenerate. **Crucially, the value is NOT echoed back into a response header** — `send_with_cid` echoes the *post-validation* `cid` (`uuid.uuid4()` or the parsed UUID). So no header smuggling on the response. ✅ | OK |
| 10 KiB / 1 MiB header | regenerate | regex fails immediately on first non-matching char (`re.match`, anchored, `^[0-9a-f]{8}…`); Python's `re` does not have catastrophic backtracking here because the pattern is linear. Still, `decode("ascii", errors="ignore")` allocates a 1 MiB string, and the regex walks it until first mismatch. Practically O(1). However the inbound header bytes are still buffered in scope — concern is the ASGI server, not the middleware. | OK at middleware; **see F2** below for byte budget. |
| Non-ASCII (emoji, RTL, BOM) | regenerate | `decode("ascii", errors="ignore")` silently drops non-ASCII bytes → leftover (possibly empty) string → no UUID match → regenerate | OK functionally, but **silent data corruption is logged-as-empty** (cf. F1) |
| Multiple `X-Correlation-ID` headers | undefined; safest = regenerate | `dict(scope["headers"])` keeps **only the LAST occurrence** (dict overwrite). The earlier value is discarded. If the latter is malformed but the former was a valid UUID, the valid one is silently dropped. | **F1** (low-sev) |
| Already-valid v4 UUID | echo as-is | regex matches → `cid = cid_in` → echoed in response header | OK |
| Garbage string | regenerate | regex fails → regenerate | OK |
| Near-valid UUID (off-by-one char, wrong variant nibble e.g. `7` not `[89ab]`) | regenerate | Strict regex enforces v4 + variant nibble → regenerate | OK |
| Lowercase vs uppercase v4 | echo | regex `re.IGNORECASE` → both match | OK |

### A — Notes
The middleware's biggest *behavioral* risk would be reflecting the input header into a response header (CRLF smuggling). It does **not** do this — only the validated/regenerated `cid` is emitted, base64-safe by construction. ✅

The risk that does exist is in the **log line**: `log.info("api.request.received", … correlation_id=cid)`. Since `cid` is the post-validation value (UUID or freshly generated), there's no log injection here either. ✅

But `cid_in` is silently mangled when non-ASCII bytes are stripped, and only the *last* of duplicate headers wins. Both are minor (the regenerated UUID is correct), but client-supplied IDs that are invalid for non-obvious reasons silently get replaced. This complicates debugging.

---

## Vector B: Authorization

| Input | Expected | Actual | Gap? |
|---|---|---|---|
| missing | 401 missing_header | `auth_header == ""` → 401 missing_header | OK |
| empty `Authorization:` | 401 missing_header | decode empty → falsy → 401 missing_header | OK |
| `"Bearer"` (no space, no token) | 401 malformed | does not start with `"Bearer "` (with trailing space) → 401 malformed_header | OK |
| `"Bearer "` (just the prefix + space) | 401 malformed | passes `startswith("Bearer ")`, slice → `""`, strip → `""`, falsy → 401 malformed_header | OK |
| `"Bearer  token"` (double space) | 401 malformed_header (RFC 6750 disallows extra LWS) **or** accept? | passes `startswith("Bearer ")`, slice → `" token"`, `.strip()` → `"token"` → compared to expected. **The leading whitespace is silently absorbed.** A client that sends `"Bearer …"` (NBSP) wouldn't match because NBSP isn't in `str.strip()`'s default set… wait, **NBSP IS stripped** by Python's `str.strip()` (it considers Unicode whitespace). However, NBSP isn't ASCII, so `decode("ascii", errors="ignore")` would have already dropped it. | **F2** — `"Bearer  token"` passes auth identically to `"Bearer token"`. Likely benign but weakens the "exactly one space" reading of RFC 6750. |
| `"bearer <token>"` (lowercase) | 401 — RFC says case-insensitive scheme match, but spec §5.4 evidently mandates `Bearer ` literal | 401 malformed (does not start with `"Bearer "`). **This is non-compliant with RFC 7235 §2.1**, which says "the [scheme] is matched case-insensitively". | **F3** — RFC compliance gap. Severity depends on whether the spec explicitly mandates strict casing. |
| `"BEARER <token>"` | same as above | 401 malformed | **F3** |
| `"Basic <b64>"` | 401 | does not start with `"Bearer "` → 401 malformed | OK |
| `"Bearer <token>\r\n..."` header smuggling | 401 or reject | `\r\n` in a header value is normally rejected by the HTTP parser (h11) before reaching ASGI scope. If somehow injected raw, `.strip()` removes trailing `\r\n` whitespace, but the slice `auth_header[len("Bearer "):]` includes whatever follows. If `\r\n` survived into the bytes object, `.strip()` would remove it, leaving a clean token. **The token never gets echoed into a response header** so no smuggling reflection. | OK |
| Multiple `Authorization` headers | 401 (RFC says reject) | dict-merge keeps **last** only. If attacker can inject a *second* header with a valid token, they win. **In practice, h11/httptools/most ASGI servers raise on duplicate Authorization** but that's transport-dependent. | **F4** — relies on transport-layer dedup. |
| Token with internal whitespace `"Bearer abc def"` | 401 (token contains space — invalid Bearer per RFC 6750 BNF `b64token`) | slice → `"abc def"`, strip → `"abc def"`, compare to expected: fails → 401 bad_token. | OK in effect (rejected), but reason is "wrong token" not "malformed", which could mislead in logs. |
| Token == expected | 200/etc. | `compare_digest` true → pass | OK |
| Token == expected + trailing whitespace | 401 (per RFC) **or** accept (if we want to be liberal) | `.strip()` removes trailing whitespace → matches → pass. **Non-strict.** Same bucket as F2. | F2 |
| Token containing non-ASCII (e.g. UTF-8 secret) | safe | `decode("ascii", errors="ignore")` SILENTLY DROPS those bytes. The resulting token is shorter than what the client sent. Then it's `.encode("utf-8")` (re-encoded ASCII subset) and compared. **If the legit token is non-ASCII**, no client could ever authenticate. Settings validator strips whitespace but doesn't forbid non-ASCII tokens. | **F5** — input-output asymmetry: server stores non-ASCII token, client sends it, middleware silently truncates it during compare. Only matters if operator picks a non-ASCII token; the doc says "opaque" so they could. |
| Token that, when sha256'd, collides on first 8 hex chars with a known token | log fingerprint collision | Pure cosmetic; sha256 prefix is for debugging not security. | non-finding |
| `compare_digest` against `expected.encode("utf-8")` | constant-time | OK; both sides fully encoded before compare. | OK |
| Bearer token = exactly 32 ASCII bytes vs longer | OK | length-agnostic compare | OK (note: `compare_digest` early-returns False if lengths differ, but does NOT short-circuit per-byte) |

### B — Notes
The most interesting input-validation gap is **F5** (silent ASCII truncation of non-ASCII tokens). It's not exploitable on its own (no privilege gained), but it produces an unauthenticatable state for any operator who configures a non-ASCII token. Either harden the settings validator to forbid non-ASCII, or fix the middleware to use `errors="strict"` and reject on `UnicodeDecodeError` with 401 malformed. Strict + reject is safer (defense in depth).

---

## Vector C: Content-Length

| Input | Expected | Actual | Gap? |
|---|---|---|---|
| missing | proceed; streaming path enforces cap | `cl_bytes is None` → skip CL path → wrap receive with cap | OK |
| `"0"` | proceed; allow 0-byte body | `int("0")=0`; `0 > cap` is False → proceed | OK |
| `"-1"` | reject (RFC: malformed) | `int("-1")=-1`; `-1 > cap` is False → **proceeds with the request**. The negative CL is *swallowed*. | **F6** — passes a malformed CL through to the next layer instead of returning 400. Most ASGI servers (h11) reject this at parse-time, but the middleware doesn't defend in depth. |
| `"abc"` | reject (RFC malformed) | `ValueError` caught → `cl = 0` → proceeds. | **F6** — silently degrades a malformed request to "no CL, use streaming cap". Functionally safe (cap still enforced) but masks a clear protocol violation. |
| `"1.5"` | reject | same as `"abc"` — `int("1.5")` raises ValueError → cl=0 → proceeds | **F6** |
| `"1e6"` | reject | same — ValueError → cl=0 → proceeds | **F6** |
| `"32768"` (exactly cap) | proceed | `cl=32768; 32768 > 32768` False → proceed (200/whatever) | OK |
| `"32769"` (cap+1) | 413 | `32769 > 32768` True → 413 emitted | OK |
| `""` (empty CL) | reject | `int("")` raises ValueError → cl=0 → proceed | F6 |
| Duplicate CL headers same value | accept (RFC 7230 §3.3.2) | dict-merge keeps last → effectively one CL → proceed | OK at middleware (transport layer should normalize) |
| Duplicate CL headers different values | RFC says **reject** (request smuggling) | dict-merge keeps last; **the conflict is never detected**. Defense-in-depth gap if the upstream parser doesn't catch it. | **F7** — request smuggling defense relies entirely on transport (h11). Not the middleware's primary job, but no defense-in-depth. |
| CL + `Transfer-Encoding: chunked` | RFC says **strip CL or reject** (smuggling) | Middleware honors CL if present. If transport strips it, fine; if not, the CL path may permit a streaming body that exceeds cap when chunks aggregate. **However, the streaming receive() wrapper is ALWAYS attached after the CL check**, so even if CL says 100 and chunks deliver 100KiB, the streaming counter trips → 413. ✅ Defense in depth here works. | OK |
| CL much larger than int64 (e.g. `"99999999999999999999"`) | proceed cap-rejected | `int("9..."*20)` returns a huge Python int (no overflow) → > cap → 413 | OK |
| CL with leading + (`"+100"`) | accept | `int("+100")=100` ✅ proceed | OK |
| CL with leading whitespace (`" 100"`) | RFC says no LWS in CL; reject | `int(" 100")=100` (Python tolerates) → proceed | minor F6 — Python's `int()` is more permissive than RFC; not a security issue but a strict-parser would reject. |

### C — Notes
The pattern in **F6** is that any malformed CL is silently downgraded to "use the streaming path" instead of returning 400. Functionally safe because the streaming cap still triggers, but a strict parser is what spec-compliant proxies expect. Operators chasing a smuggling bug would benefit from a real `400 Bad Request` here.

---

## Vector D: Origin (CORS)

CORS handling is delegated to `starlette.middleware.cors.CORSMiddleware` with:
```
allow_origins=settings.cors_origins,
allow_credentials=False,
allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
expose_headers=["X-Correlation-ID"],
```

| Input | Expected | Actual | Gap? |
|---|---|---|---|
| missing Origin | no ACAO header echoed | starlette behavior: no ACAO header → OK | OK |
| empty Origin | no echo | starlette compares against allow list; empty match unlikely → no echo | OK |
| Multiple Origin headers | no echo (per spec, only one allowed) | dict-merge keeps last; only that one is checked | OK at middleware; UA already disallows |
| Origin in cors_origins | echo allowed origin | starlette echoes exact match | OK |
| Origin NOT in cors_origins | no echo | starlette refuses | OK |
| Port mismatch | no echo (port is part of origin) | exact match required → no echo | OK |
| Trailing slash | no echo (per CORS spec, origins don't have trailing slash) | exact-string compare → no echo | OK (and operator must not configure with trailing slash either, since `_reject_empty_cors_origin` only checks empty, not URL-shape) — **F8** small operator footgun |
| Non-ASCII / IDN origin | per spec, ACAO must be ASCII | starlette echoes header verbatim → if operator put a non-ASCII origin in cors_origins, ACAO leaks non-ASCII into response. **Settings validator does not enforce ASCII on cors_origins.** | **F8** — config validation gap, low severity (operator-initiated). |
| `"null"` Origin | no echo unless explicitly allowed (rare) | starlette compares "null" to list → only echoed if `"null"` is in cors_origins. ✅ | OK |
| Origin from privileged path (browser-spoofed by curl) | (no enforcement; CORS is client-side) | as expected — server-to-server has no Origin | OK |

### D — Notes
CORS surface is minimal and correctly scoped. The only thing worth tightening is config-time validation of `cors_origins` shape (no trailing slash, ASCII-only, scheme://host[:port] pattern). **F8.**

---

## Vector E: AUTH_EXEMPT_PREFIXES path probes

`any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)` where prefixes are:
- `/api/v1/health`
- `/api/v1/openapi.json`
- `/api/v1/docs`
- `/api/v1/redoc`

| Input path | Auth-exempt? | Reaches a handler? | Gap? |
|---|---|---|---|
| `/api/v1/health` | YES (canonical) | health handler → 200/503 | OK |
| `/api/v1/health/` | YES (`startswith` matches) | FastAPI does **not** auto-strip trailing slash by default — but `/api/v1/health/` would be a 404. Auth bypass + 404 is harmless. | OK |
| `/api/v1/healthx` | **YES — startswith catches `/api/v1/healthx`** because `"/api/v1/healthx".startswith("/api/v1/health")` is True. Auth bypassed. **No handler at this path → 404.** Harmless today, but **if BL6+ ever mounts a `/api/v1/healthxxx` admin route, it bypasses auth.** | 404 today | **F9 — SEV-2 latent risk.** Prefix should be `/api/v1/health` exact OR `/api/v1/health/`. Any future router prefix beginning with `/api/v1/health…` becomes accidentally public. Same applies to `/api/v1/docs` (e.g. `/api/v1/docszzz`), `/api/v1/redoc`, and `/api/v1/openapi.jsonxx`. |
| `/api/v1/health/../whatever` | YES (startswith); FastAPI/starlette **does not normalize `..` segments**. ASGI scope's `path` is the raw path. The auth check passes; routing falls through to whatever handler matches the literal path. Most likely 404. | 404 | OK in practice today. **If a future router lives at `/api/v1/whatever`, this would auth-bypass it.** **F9** generalization. |
| `/api/v1/health%2f..%2fwhatever` | the URL-encoded form is decoded by the ASGI server before scope is built. So `path` contains `/api/v1/health/../whatever` — which still starts with `/api/v1/health` → auth bypass. (Most ASGI servers reject `%2f` in paths or normalize them; depends on uvicorn config — `--no-decode` is not default.) | 404 likely | **F9** |
| `/api/v1/HEALTH` | NO (`startswith` is case-sensitive) → auth required → 401 | 401 | OK |
| `/api/v1//health` | NO (does not literally start with `/api/v1/health` — extra slash) → 401 | 401 | OK; but operator confusion since this is semantically the same path |
| `/openapi.json` (no prefix) | NO — auth required. (Note: openapi is mounted at `/api/v1/openapi.json` per `create_app`) | 401 | OK |
| `/docs`, `/redoc` (no prefix) | NO | 401 | OK |

### E — Critical finding
**F9** is the standout: `path.startswith("/api/v1/health")` matches `/api/v1/healthxxxxx`. Today there's no handler there, so 404. But this is an *auth-bypass primitive* waiting for a future router prefix collision. Should be tightened to either:
- exact match: `path in AUTH_EXEMPT_PATHS or path.startswith(p + "/") for p in EXEMPT_PREFIXES`
- or: `path == p or path.startswith(p + "/")`

Severity SEV-2 because BL5 has no exposed bypass, but it's a footgun for BL6+.

---

## Vector F: LOOPBACK_ONLY_PATTERNS

Pattern: `^/api/v1/platforms/[^/]+/auth$` (anchored, single path segment for platform name).

| Input | Expected | Actual | Gap? |
|---|---|---|---|
| canonical `/api/v1/platforms/steam/auth` from 127.0.0.1 | pass | `client_host=="127.0.0.1"` → pass | OK |
| trailing slash `/api/v1/platforms/steam/auth/` | regex fails (anchored `$`) → loopback check skipped → **request reaches the handler regardless of client IP**. If a route exists at `…/auth/`, it's accessible from external IP. | depends on route table | **F10 — SEV-2 latent.** The loopback gate is path-shaped; near-misses bypass it. Combined with FastAPI's default of redirecting trailing slashes (`redirect_slashes=True`), an external client hitting `…/auth/` may be redirected (308) to `…/auth` — but the redirect happens AFTER middleware. Need to verify on real router config; today no `…/auth` route exists yet so this is theoretical. |
| double slash `/api/v1/platforms//auth` | regex fails (`[^/]+` requires ≥1 char) → loopback check skipped → reaches handler if any | depends on route | **F10** |
| URL-encoded segment `/api/v1/platforms/%73team/auth` | server may decode → `…/steam/auth` matches → loopback enforced; or may not decode → no match → loopback skipped | transport-dependent | **F10** |
| `[::1]` IPv6 loopback | `client_host == "::1"` ≠ `"127.0.0.1"` → 403 | 403 | **F11** — IPv6 loopback is rejected. Likely intended (Bible §7.3 says `127.0.0.1` only) but worth confirming with spec. |
| `::ffff:127.0.0.1` IPv4-mapped IPv6 | depends on transport: some servers report `"127.0.0.1"`, some report `"::ffff:127.0.0.1"`. If the latter, 403. | transport-dependent | **F11** |
| Reverse-proxy spoofed `X-Forwarded-For: 127.0.0.1` from external client | should NOT be trusted | middleware reads `scope["client"]`, NOT `X-Forwarded-For`. ✅ Even if the header is present, it's ignored. | OK — correct |
| `scope["client"] is None` | reject | `client_info = None` → `client_host = None` → not `"127.0.0.1"` → 403 | OK |
| `scope["client"] = ("127.0.0.1", 0)` | accept | tuple[0] == "127.0.0.1" → pass | OK |
| `scope["client"] = ("0.0.0.0", 0)` | reject | host != "127.0.0.1" → 403 | OK |
| `scope["client"] = ("", 0)` (empty string) | reject | "" != "127.0.0.1" → 403 | OK |

### F — Notes
The loopback gate correctly ignores `X-Forwarded-For` (the right call). The risk is that the gate is path-shape-coupled (regex anchored on a specific shape). Any handler reached via an unexpected path shape (`…/auth/`, `//auth`, encoded variants) skips the gate entirely. Today no `…/auth` handler exists, so this is latent. Mitigations to consider:
1. After auth and loopback gates, add a **per-handler** decorator that re-asserts loopback for handlers that need it (defense in depth).
2. Or do an early path-normalization step.

Severity SEV-2 latent — **F10**.

---

## Vector G: HealthResponse extra="forbid"

```python
class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status, version, uptime_sec, scheduler_running, lancache_reachable,
    cache_volume_mounted, validator_healthy, git_sha
```

The handler does:
```python
body = HealthResponse(status=…, version=…, …)
return JSONResponse(content=body.model_dump(), status_code=…)
```

Failure mode if a future BL passes an extra kwarg via dict-spread (e.g. `HealthResponse(**status_dict)` where status_dict has an unknown key):

- pydantic raises `ValidationError` at construction time with `type=extra_forbidden`.
- Inside an async FastAPI handler, an unhandled exception → starlette returns **500 Internal Server Error** with no body, plus structlog logs the traceback.
- **Concrete failure mode:** /health returns 500 instead of 200/503. This means the **health endpoint itself becomes unhealthy on a code typo** — bad signal coupling.
- Log signature: `pydantic_core._pydantic_core.ValidationError: 1 validation error for HealthResponse\n<field>\n  Extra inputs are not permitted [type=extra_forbidden, input_value=…, input_type=…]`. The `input_value` could include the offending value verbatim — **if a future BL puts a sensitive value there (e.g. from a settings field), it leaks in the traceback**.

| Scenario | Actual | Gap? |
|---|---|---|
| BL adds field to dict, forgets model | 500 from /health, traceback in stderr/log | **F12 — SEV-3.** /health returning 500 from a typo is a foot-gun. |
| dict-spread passes secret via unknown key | 500 + secret in traceback | **F12** + secret leak |
| Field type wrong (e.g. uptime_sec = "abc") | ValidationError → 500 (independent of extra="forbid") | normal pydantic behavior |
| status not in {"ok", "degraded"} | ValidationError → 500 | OK by design |

### G — Notes
`extra="forbid"` is correct for input-side validation (request DTOs). For response models it's debatable — strict-on-egress catches dev-time typos but yields 500s in prod. A safer pattern for response models is `extra="ignore"` + a unit test that ensures the dict matches the schema exactly. Or wrap the construction in a try/except that returns a degraded health response on schema mismatch.

---

## Vector H: Body cap edge cases (deep dive)

| Scenario | Expected | Actual | Gap? |
|---|---|---|---|
| Body=0, no CL header | proceed | no CL → streaming path, `bytes_received=0`, never trips cap → proceed | OK |
| Body = exactly cap, no CL header (chunked) | proceed | streaming counter reaches 32768; check is `> cap` (strict greater); 32768 > 32768 False → proceed | OK |
| Body = cap+1 byte, no CL header (chunked) | 413 | streaming counter hits 32769 in some chunk; > 32768 → raises `_BodyTooLargeError` → 413 | OK |
| Client disconnects mid-stream | downstream sees `http.disconnect` | `receive_with_cap` calls underlying receive, which yields `{"type": "http.disconnect"}`. The wrapper's `if msg["type"] == "http.request"` branch is skipped, byte counter unchanged, msg returned upstream as-is. **No exception, no 413, no log line.** Whatever downstream does with disconnect (FastAPI cancels the task) is unmodified. | OK |
| `more_body: true` chunks indefinitely under cap each | total accumulates → trips cap | counter is monotonic across chunks; eventually > cap → 413 raised | OK |
| Two concurrent body-cap rejections from same client | independent middleware instances per request | `BodySizeCapMiddleware.__call__` uses local `bytes_received` (closure on `nonlocal`) per request. **No shared mutable state.** Module-level `_log` is fine (structlog is async-safe). | OK |
| Sender includes a single chunk of 1 GiB with `more_body: false` and no CL | should 413 fast | `bytes_received += len(body)` allocates the full 1 GiB chunk in memory FIRST, *then* the cap check runs and raises. **OOM possible** if attacker sends a single huge chunk. The body bytes were already read into memory by the ASGI server; receive() returns the bytes object. The middleware checks AFTER. | **F13 — SEV-2.** Single-chunk DoS: an attacker can make uvicorn buffer up to its own per-message limit (default 64 KiB for h11, can be tuned higher) and the middleware doesn't reject before allocation. Mitigation: check `len(body)` against cap on each chunk *individually* in addition to the running total. |
| CL=100, chunks deliver 100 KiB | CL says proceed; streaming should still trip | After CL check (100 ≤ cap, proceed), streaming wrapper still tracks. Eventually trips cap → 413. ✅ Defense-in-depth works. | OK |
| Receive raises on transport error mid-stream | downstream gets the exception | `receive_with_cap` does not wrap receive() in try/except — so any transport-layer exception propagates up. Then app-level `except _BodyTooLargeError` does NOT catch it; the generic exception bubbles up to starlette's exception handler → 500. | OK (correct propagation), but **note**: a malformed Content-Length that *the transport layer doesn't reject* but DOES cause receive() to return inconsistent data could subtly mislead the cap counter. Hard to construct in practice. |
| Two ASGI lifespan messages reaching middleware (`scope["type"]=="lifespan"`) | pass through unchanged | `if scope["type"] != "http": app(scope, receive, send); return` — handled at top of every middleware. ✅ | OK |

### H — Notes
**F13 (single-chunk DoS)** is the real find. The cap check is *after* the bytes are in scope/memory. For practical exploitation, an attacker would need the ASGI server to deliver more than ~32 KiB in a single `http.request` message — uvicorn/h11 typically chunks at smaller sizes, so this is a config-dependent risk, not a guaranteed exploit. Still, defense in depth says: check chunk size on arrival.

---

## Findings

### SEV-1
None.

### SEV-2
- **F9 (auth-exempt prefix substring match):** `path.startswith("/api/v1/health")` matches `/api/v1/healthxxx`. Today: 404. Tomorrow: a router prefix collision becomes an unauth-required public path. Recommend tightening to `path == p or path.startswith(p + "/")`. Affects all four AUTH_EXEMPT_PREFIXES.
- **F10 (loopback gate path-shape coupling):** Regex `^/api/v1/platforms/[^/]+/auth$` does not match `…/auth/`, `…//auth`, encoded variants. If a future handler is reachable via any of these path shapes, the loopback gate is silently skipped. Mitigation: per-handler defense-in-depth, or normalize path before matching.
- **F13 (single-chunk DoS in body cap):** `bytes_received += len(body)` accumulates *after* the chunk is in memory. A single oversized chunk allocates memory before being rejected. Recommend per-chunk size check at receive time.

### SEV-3
- **F5 (silent ASCII truncation of non-ASCII auth tokens):** `decode("ascii", errors="ignore")` silently drops non-ASCII bytes. Operators who configure a non-ASCII token cannot authenticate, and the failure mode is "wrong token" not "malformed". Either reject non-ASCII at settings load or use `errors="strict"` and 401 on `UnicodeDecodeError`.
- **F12 (HealthResponse extra="forbid" → 500 on typo):** A future BL that adds a key to the response dict but not the model causes /health to return 500 — and the failing field's value lands in the traceback (potential leak). Recommend either extra="ignore" + a unit test, or wrap construction with a fallback degraded response.

### SEV-4
- **F1 (correlation-id silent mangling):** Non-ASCII bytes silently dropped via `errors="ignore"`. Multi-header occurrences keep last-only. Cosmetic; debugging-only impact.
- **F2 (Bearer extra-whitespace tolerance):** `"Bearer  token"`, `"Bearer token "` accepted via `.strip()`. RFC says exactly one space + b64token. Cosmetic.
- **F3 (case-insensitive scheme):** `bearer`, `BEARER` rejected as malformed. RFC 7235 §2.1 requires case-insensitive scheme matching. Compliance gap.
- **F4 (multiple Authorization headers — last wins):** dict-merge keeps last; transport layer is the only safety net.
- **F6 (malformed Content-Length silently degraded):** `"abc"`, `"-1"`, `"1.5"`, empty CL → all become `cl=0` and pass to streaming path. Functionally safe (cap still enforced) but masks protocol violations.
- **F7 (no defense in depth on duplicate CL or CL+TE smuggling):** middleware relies on transport for smuggling rejection.
- **F8 (CORS origin shape not validated at config load):** `cors_origins` accepts trailing slashes, non-ASCII strings, anything non-empty. Operator footgun.
- **F11 (IPv6 loopback rejected):** `::1` and `::ffff:127.0.0.1` → 403. May be intentional per spec but worth explicit confirmation.

### Regression test sketches

```python
# F9: AUTH_EXEMPT prefix-collision protection
@pytest.mark.parametrize("path", [
    "/api/v1/healthxxx",
    "/api/v1/healthx",
    "/api/v1/docszzz",
    "/api/v1/openapi.jsonx",
    "/api/v1/redocx",
])
async def test_auth_required_for_prefix_neighbor_paths(client, path):
    r = await client.get(path)
    assert r.status_code == 401, (
        f"{path} bypassed auth via startswith collision"
    )

# F10: loopback gate trailing-slash bypass
async def test_loopback_gate_path_normalization(external_client):
    for path in [
        "/api/v1/platforms/steam/auth/",
        "/api/v1/platforms//auth",
        "/api/v1/platforms/steam/auth/x",
    ]:
        r = await external_client.post(
            path,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # External client must never reach a body-handler at any
        # variant of the loopback-gated path
        assert r.status_code in (401, 403, 404), (
            f"{path} skipped loopback gate from external IP"
        )

# F13: single-chunk body-cap DoS
async def test_body_cap_rejects_oversized_single_chunk_before_accumulation():
    # Build a single chunk > cap, no CL header, more_body=False.
    # Verify 413 is sent and downstream is never called.
    ...

# F5: non-ASCII token rejected as malformed (not bad_token)
async def test_non_ascii_authorization_token_returns_malformed_401():
    r = await client.get(
        "/api/v1/anything",
        headers={"Authorization": "Bearer tökën"},
    )
    assert r.status_code == 401
    # And log event reason should be "malformed_header", not "bad_token"

# F3: RFC 7235 case-insensitive scheme
@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
async def test_bearer_scheme_case_insensitive(scheme):
    r = await client.get(
        "/api/v1/anything",
        headers={"Authorization": f"{scheme} {VALID_TOKEN}"},
    )
    assert r.status_code == 404  # auth passes, route is the 404

# F12: extra-forbid on response model 500-mode
async def test_health_handler_resilient_to_extra_field_drift(monkeypatch, client):
    # Monkeypatch HealthResponse to simulate a future BL adding an unknown
    # field via dict-spread; verify /health doesn't 500.
    ...

# F6: malformed Content-Length returns 400, not silent degrade
@pytest.mark.parametrize("cl", ["-1", "abc", "1.5", "1e6", ""])
async def test_malformed_content_length_returns_400(client, cl):
    r = await client.post(
        "/api/v1/anything",
        content=b"x",
        headers={"Content-Length": cl},
    )
    assert r.status_code == 400
```

---

## Non-findings

- **CRLF injection in X-Correlation-ID:** middleware never reflects `cid_in` into a response header. Only the validated `cid` (which is always either a regex-matched UUID or `uuid.uuid4()`) is echoed. No log line uses `cid_in` either. Safe.
- **Token leakage via log fingerprint:** sha256 prefix is 8 hex chars (32 bits); not reversible. Log-redaction bypass via field-name choice (`rejection_fingerprint` instead of `token_*`) is documented in the code comment and is the intended design.
- **`hmac.compare_digest` correctness:** both sides are utf-8-encoded bytes of equal-or-mismatched length; constant-time compare fires correctly. Even with empty strings, `compare_digest` short-circuits cleanly.
- **Concurrent body-cap rejection state:** `bytes_received` is a closure-local nonlocal in `__call__`. No shared mutable state. Two concurrent requests get independent counters.
- **CL + chunked smuggling as middleware-level concern:** Defense in depth via the streaming wrapper means even if CL lies about the size, the streaming path still trips the cap. Smuggling defense at the protocol layer is uvicorn/h11's job; middleware doesn't introduce a new gap.
- **X-Forwarded-For loopback bypass:** middleware reads `scope["client"]`, not headers. ✅
- **`compare_digest` timing on length-mismatched tokens:** While `compare_digest` is documented to short-circuit on length mismatch (returns False), it does so **without** revealing per-byte timing — this is intentional and safe.
- **CorrelationId regex catastrophic backtracking:** the pattern is linear (no nested quantifiers, no overlapping alternations). A 1 MiB string fails on first non-hex byte; O(1) practical.
- **Missing/empty Origin under CORS:** starlette behaves correctly; no echo, no leak.
- **OPTIONS preflight bypass of auth:** intentional per CORS spec. Documented in code (`if method == "OPTIONS": skip auth`). Not a finding.
- **Loopback gate ignores X-Forwarded-For:** correctly the right call. Not a gap.
- **dict-merge of headers keeping last value:** ASGI servers normalize duplicate headers per RFC 7230 §3.2.2 (combine with comma) for most headers, or reject for specific ones (Authorization, Host). The middleware's behavior is the standard ASGI pattern. Not a primary concern.
