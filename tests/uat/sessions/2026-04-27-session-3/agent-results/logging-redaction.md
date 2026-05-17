# UAT-3 Logging-Redaction Empirical Audit
**Agent:** logging-redaction
**Date:** 2026-04-27
**Branch:** feat/uat-3-session
**Scope:** BL5 logging surface — middleware, app factory, routers, dependencies, against `_SENSITIVE_KEY_RE` in `src/orchestrator/core/logging.py`.

Method: read every `_log.*` / `log.*` call site, enumerate kwargs, run the regex against each kwarg key in a Python REPL, exercise the actual rendered output for the rejection path end-to-end, and probe `_redact_sensitive_values` for recursion gaps with hand-crafted shapes.

---

## A: Log-call inventory

| File:Line | Event | Severity | Kwargs (keys only) | Sensitive kwargs (any?) | Regex correctness |
|---|---|---|---|---|---|
| `api/main.py:45` | `api.boot.migrations_starting` | info | (none) | none | n/a |
| `api/main.py:49` | `api.boot.migrations_failed` | critical | `reason` (str of `MigrationError`) | none — exception text only, no creds in `MigrationError` per `db/migrate.py` | OK |
| `api/main.py:53` | `api.boot.pool_starting` | info | (none) | none | n/a |
| `api/main.py:57` | `api.boot.pool_init_failed` | critical | `reason` (str of pool exc) | none — pool errors carry path/migration version, not creds | OK |
| `api/main.py:63` | `api.boot.complete` | info | (none) | none | n/a |
| `api/main.py:67` | `api.shutdown.starting` | info | (none) | none | n/a |
| `api/main.py:71` | `api.shutdown.pool_close_failed` | error | `reason` | none | OK |
| `api/main.py:72` | `api.shutdown.complete` | info | (none) | none | n/a |
| `api/middleware.py:72` | `api.request.received` | info | `method`, `path`, `correlation_id` | none — `path` is request path string (no embedded creds in BL5 routes); `correlation_id` is reserved/owned, intentional | OK |
| `api/middleware.py:90` | `api.request.completed` | info | `duration_ms`, `correlation_id` | none | OK |
| `api/middleware.py:126` | `api.body_size_cap_exceeded` (Content-Length path) | error | `path`, `content_length`, `cap` | none | OK |
| `api/middleware.py:151` | `api.body_size_cap_exceeded` (streaming path) | error | `path`, `bytes_received`, `cap` | none | OK |
| `api/middleware.py:207` | `api.auth.rejected` | warn | `reason="missing_header"`, `path` | none — header absent | OK |
| `api/middleware.py:212` | `api.auth.rejected` | warn | `reason="malformed_header"`, `path` | none — Authorization value NOT logged | OK |
| `api/middleware.py:218` | `api.auth.rejected` | warn | `reason="malformed_header"`, `path` | none — empty-token branch | OK |
| `api/middleware.py:231` | `api.auth.rejected` | warn | `reason="bad_token"`, `path`, `rejection_fingerprint` | `rejection_fingerprint` value is sha256[:8] hex of submitted token — non-reversible; safe to log | OK (intentionally non-matching key) |
| `api/middleware.py:245` | `api.auth.rejected` | warn | `reason="non_loopback"`, `path`, `client_host` | none | OK |
| `api/routers/health.py` | (no log calls) | — | — | — | — |
| `api/dependencies.py` | (no log calls) | — | — | — | — |

Verified by running `_SENSITIVE_KEY_RE.search(key)` on every kwarg name above. Match results — all `False` for BL5-introduced keys, including the load-bearing `rejection_fingerprint`.

---

## B: rejection_fingerprint propagation

End-to-end trace executed live (REPL captured output):

1. Request arrives at `BearerAuthMiddleware.__call__` with `Authorization: Bearer wrong-token-…`.
2. `hmac.compare_digest` mismatches at `middleware.py:224`.
3. `sha = hashlib.sha256(token.encode()).hexdigest()[:8]` → e.g. `"b996d87d"`.
4. `_log.warning("api.auth.rejected", reason="bad_token", path=path, rejection_fingerprint=sha)`.
5. structlog pipeline runs (`configure_logging` order):
   `merge_contextvars` → `_protect_reserved_keys` → `add_log_level` → `StackInfoRenderer` → `set_exc_info` → `TimeStamper` → `format_exc_info` → **`_redact_sensitive_values`** → `JSONRenderer`.
6. In `_redact_sensitive_values`, `_walk` iterates the event_dict; for key `"rejection_fingerprint"` it calls `_SENSITIVE_KEY_RE.search("rejection_fingerprint")`. The regex token alternation includes neither `rejection`, `fingerprint`, nor anything that substring-matches them. Result: `False` — value passes through untouched.
7. Captured live output:
   ```json
   {"reason": "bad_token", "path": "/api/v1/x", "rejection_fingerprint": "b996d87d",
    "event": "api.auth.rejected", "correlation_id": "aaaa…aaaa",
    "level": "warning", "timestamp": "2026-04-27T23:33:45.823759Z"}
   ```

**Verdict:** `rejection_fingerprint` is correctly emitted in plaintext, with correlation_id propagation from the surrounding `request_context`. No regression.

The rename from `token_sha256_prefix` → `rejection_fingerprint` is the only thing keeping this field out of the redactor; the comment at `middleware.py:226-230` documents why and is accurate.

---

## C: Authorization-leakage paths

Exhaustive enumeration of code paths that could echo the raw `Authorization` header:

1. **`BearerAuthMiddleware` — header read at line 204.** `auth_header` is a local variable. It is referenced at lines 207, 212, 216, 218 but **never logged**. Only the SHA-256 prefix of the bearer portion (`token`) is logged at 231. Verdict: clean.
2. **CorrelationIdMiddleware (lines 59–94)** reads only the `x-correlation-id` header byte-string. Never reads or logs `Authorization`. Logs `method` + `path` only.
3. **BodySizeCapMiddleware (lines 111–157)** reads only `content-length`. Logs `path`, `content_length`, `cap`, `bytes_received`. Never touches `Authorization`.
4. **structlog default extractors:** structlog has no default ASGI scope/header extractor; the `merge_contextvars` processor pulls from `structlog.contextvars` only, which in this codebase contains exactly one binding (`correlation_id`) set by `request_context`. No automatic header pull. Verdict: clean.
5. **FastAPI default exception handler** (Starlette `ServerErrorMiddleware`): on uncaught 500, FastAPI returns `{"detail":"Internal Server Error"}` and logs the traceback via the `uvicorn.error` logger — that handler does **not** include request headers in the message. The traceback formatter (`logging.Formatter`) renders only the exception object, not request scope. Verdict: clean unless a handler explicitly puts the header into the exception args.
6. **uvicorn access log:** uvicorn's access log format is `'%(client_addr)s - "%(request_line)s" %(status_code)s'` by default (no headers). The request_line is `METHOD /path HTTP/1.1` — Authorization not included. Verdict: clean by uvicorn default; would need explicit `--access-log-format` override to leak.
7. **request_context kwargs:** only `correlation_id` is bound. No header bleed.
8. **CORS middleware:** Starlette's `CORSMiddleware` does not emit logs on the success path. On a preflight reject it does not log either. Verdict: clean.
9. **413 / 401 / 403 response bodies:** static byte-strings. No header echo.

**Single residual risk (DEFERRED, not BL5):** if a future endpoint catches an exception and `log.error("...", request=request)` or `log.error("...", scope=scope)`, the ASGI `scope["headers"]` list (a list of `(bytes, bytes)` tuples) would be logged. The current `_redact_sensitive_values` walker handles dicts/lists/tuples, BUT for a tuple of `(b"authorization", b"Bearer xxx")` the `b"authorization"` is the *value at index 0*, not a dict-key; `_walk` only redacts dict keys. **The header would be logged in plaintext.** No BL5 site does this today (verified above), but the redactor cannot save us if anyone adopts that pattern. Filed as SEV-3 below.

**Verdict:** No Authorization leak in BL5 as shipped. One latent shape gap in the redactor (tuple-of-bytes headers) creates a future foot-gun.

---

## D: correlation_id propagation

`CorrelationIdMiddleware` is the OUTERMOST middleware (`main.py:112`, registered last → executes first). All other middlewares + the router run inside its `with request_context(correlation_id=cid):` block. Verified:

- `BodySizeCapMiddleware` runs inside `request_context` → its `_log.error("api.body_size_cap_exceeded", …)` calls auto-pick up `correlation_id` via `merge_contextvars`. Confirmed in code path; no test currently asserts this — gap noted below.
- `BearerAuthMiddleware` likewise inside the context → `api.auth.rejected` logs auto-include `correlation_id`. Empirically confirmed in section B.
- Router handlers (`health.py`) inside the context → would auto-pick up cid if they logged (BL5 health emits no logs).

**Out-of-context logs (no correlation_id):**
- `api/main.py` lifespan logs (`api.boot.*`, `api.shutdown.*`, lines 45/49/53/57/63/67/71/72) run during ASGI lifespan startup/shutdown — **not** inside any `request_context`. They have no correlation_id, which is correct semantically (no request to correlate with). Acceptable.
- Pre-CorrelationId-middleware code path: nothing logs before `CorrelationIdMiddleware.__call__` enters `request_context` (it's the outermost wrapper; only the ASGI server is upstream).
- Post-CorrelationId-middleware exit: the `finally` block at line 88 runs `log.info("api.request.completed", …)` BEFORE `request_context` unbinds (the `finally` is inside the `with`). Correct ordering.

**Verdict:** correlation_id propagation is correct. One small test gap: no current test asserts `api.body_size_cap_exceeded` or `api.auth.rejected` log lines carry a correlation_id. Live REPL capture above confirms it works for the auth path; recommend adding regression coverage. Filed SEV-4 below.

---

## E: Field-name conflict matrix

Regex executed against every kwarg key BL5 introduces, plus a forward-looking sweep of plausible future field names. (`_` and digits are non-letter boundaries → short-token rules fire on compounds.)

| Kwarg key | Matches regex? | Should match? | Verdict |
|---|---|---|---|
| `reason` | No | No (enumerated short string: "missing_header" / "malformed_header" / "bad_token" / "non_loopback") | OK |
| `path` | No | No (request URL path) | OK |
| `method` | No | No | OK |
| `correlation_id` | No | No (UUID4 hex; reserved; intentionally visible) | OK |
| `duration_ms` | No | No | OK |
| `content_length` | No | No | OK |
| `cap` | No | No | OK |
| `bytes_received` | No | No | OK |
| `client_host` | No | No (peer IP) | OK |
| `rejection_fingerprint` | No | No (sha256[:8] of submitted token; one-way) | OK — load-bearing |
| `applied_count`, `migration_id`, `name`, `database_path`, `readers_count`, `pragmas_applied`, `role` (lifespan-emitted from db layer) | No | No | OK |

**Future-risk forward sweep** (names a developer might plausibly add later — auto-redacted by current regex):

| Hypothetical key | Matches? | Risk if it matches a non-sensitive value |
|---|---|---|
| `token_id`, `token_count`, `token_expires_at` | YES | Same class of bug as the original `token_sha256_prefix` rename. Anything named `*token*` will be silently `<redacted>`. |
| `session_id`, `sid` | YES | A request-scoped session_id (e.g. for browser sessions) would be redacted; might be desired or not. |
| `nonce_value`, `nonce_age` | YES | CSP nonces, request nonces — non-secret in many contexts. |
| `signature_alg`, `signature_status` | YES | Algorithm names are not secrets; would be redacted. |
| `bearer_format`, `bearer_realm` | YES | Schema metadata, not secret. |
| `cookie_count`, `cookie_age_seconds` | YES | Aggregates, not secret. |
| `apikey_id`, `apikey_status`, `api_key_prefix` | YES | IDs/status non-secret. |
| `mfa_attempts`, `otp_remaining`, `pin_retries`, `pwd_age_days` | YES | Counts/metadata, not secret. |
| `salt_length`, `nonce_size` | YES | Sizes, not secret. |

**Forward-risk pattern:** the redactor is correctly aggressive (over-redaction over under-redaction is the right policy per the regex's docstring), but the cost is that any future field with a name containing `token|secret|auth|bearer|cookie|session|credential|signature|password|jwt|apikey|privkey` substring — or short-token suffix `pwd|pin|otp|mfa|tfa|sid|creds|salt|nonce` between non-letter boundaries — will be auto-redacted. That's the design, not a defect, but it is a developer foot-gun that already cost one rename. Recommendation captured as SEV-4 below.

**False-negative sweep** (sensitive concept whose name does NOT match):

| Concept-name | Matches? | Risk |
|---|---|---|
| `auth` (alone), `auth_state`, `authn`, `authz` | **NO** | `authorization` is in the regex but `auth` alone is not. A field named `auth_state="Bearer xyz"` would NOT be redacted. Today no such field exists in BL5; if added, leak. |
| `pass_through`, `passcode_hint`, `bypass` | partial — `pass` alone NOT in regex (`password`/`passwd` are full words) | `passcode_hint`: matches via `passwd`? No: regex has `passwd` as a whole substring; `passcode_hint` doesn't contain `passwd`. Would NOT be redacted. |
| `auth_header_value`, `header_authz` | partial — `header_authz` doesn't match `authorization`; `auth_header_value` doesn't match either (no `authorization` substring). | Would NOT be redacted. **Latent risk** if anyone names a field `auth_header_value`. |
| `bearer` substring missing → `bearer_value=...` matches; `headers` (plural collection) does NOT match. | Mixed | A dict named `headers` containing an `Authorization` entry IS recursively walked and the inner `authorization` key is matched — verified live above. So `headers` as a container name is safe. |

The most actionable false-negative: **`auth_*` prefix** (e.g. `auth_header`, `auth_state`, `auth_value`). The regex matches `authorization` and `bearer` but not bare `auth`. Adding bare `auth` would fire on `author`, `authentication`, `authorize`, etc. — too aggressive. Current design is acceptable but the gap exists. SEV-4 follow-up.

---

## F: Multi-level redaction

`_redact_sensitive_values._walk` is the recursive processor. Probed shapes (live REPL):

| Shape | Result |
|---|---|
| `{"request": {"headers": {"authorization": "Bearer xyz"}}}` | inner `authorization` → `<redacted>`. **Pass.** |
| `{"event": "x", "headers": [("authorization", "Bearer xyz")]}` | tuple is walked element-wise but each element is a *value*, not a key. `b"authorization"` here is the *first item of a tuple*, not a dict key. **Plaintext leak.** |
| `{"event": "h", "headers": [{"name": "authorization", "value": "Bearer xyz"}]}` | dict keys are `name` and `value` — neither matches the regex. Redactor walks the dict but nothing matches. **Plaintext leak.** |
| Cyclic dict `d = {}; d["self"] = d; log(d=d)` | `seen` set substitutes `"<cyclic>"` — verified by reading the code; cycle-safe per the docstring. **Pass.** |
| `{"event": "x", "secret": ["a","b","c"]}` | top-level key `secret` matches → entire list value replaced with `<redacted>`. **Pass.** |
| Nested key in dict: `{"outer": {"password": "p"}}` | inner `password` redacted. **Pass.** |

**Verdict:** Recursive descent works for dict-of-dict and dict-of-list-of-dict where the *dict key* is sensitive. It does **not** save us if the sensitive name is a *value* (e.g. ASGI scope's `headers` list of `(bytes, bytes)` tuples). No BL5 code emits that shape today; it is a latent foot-gun. SEV-3 below.

---

## G: f-string interpolation leaks

Search for any `f"…{token…}…"` / `"…" + token` / `% token` pattern that could embed a secret in the event name string or in a kwarg value:

- `middleware.py:225`: `sha = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]` — derived value, not the token. Safe.
- `middleware.py:231`: `rejection_fingerprint=sha` — passes the hash, not the token. Safe.
- No other f-string in `middleware.py`, `main.py`, `health.py`, or `dependencies.py` references `token`, `auth_header`, `secret`, or `password` in any string-construction context. Verified by reading every line.
- `BearerAuthMiddleware` has the raw `token` only as a local variable used in `hmac.compare_digest` and `hashlib.sha256`. It is **never** placed into a log message body, never into an exception, never into a response body.
- The 401 / 403 / 413 response bodies are static byte literals — no interpolation.

**Verdict:** No f-string or string-concatenation leak in BL5. The `_redact_sensitive_values` processor only protects against key-named credentials; it cannot intercept a sensitive value embedded inside another value's string content. Today nothing in BL5 builds such a string; if anything ever does, redaction will not catch it. (This is the standard structlog / structured-logging contract — "log structured fields, not interpolated strings".)

---

## Findings

### SEV-1
None.

### SEV-2
None.

### SEV-3 — Latent: ASGI-scope-shaped logging would bypass redaction
- **Description:** `_redact_sensitive_values._walk` matches the regex against **dict keys only**. ASGI's `scope["headers"]` is a `list[tuple[bytes, bytes]]` — pairs like `(b"authorization", b"Bearer xxx")`. If any future log call includes the raw `scope` or `scope["headers"]` (e.g. an exception handler debug-logging `scope=scope`), the Authorization header value would be emitted in plaintext.
- **Scenario:** A future BL6+ exception handler writes `_log.error("api.unhandled", scope=scope)` to aid debugging. Redactor sees a list of tuples, walks elements (returns them unchanged because none are dicts at the recursion frontier), emits the bearer token verbatim.
- **Affected code:** `src/orchestrator/core/logging.py:_redact_sensitive_values._walk` (the dict-only key check at line 181–185).
- **Fix options:**
  1. Extend `_walk` to detect `(key, value)` pair-shaped tuples in lists — when encountering `list[tuple[bytes|str, Any]]` and the first element matches the sensitive regex, redact the second. Risk: heuristic, may over-redact arbitrary 2-tuples.
  2. Alternatively, add a Semgrep / lint rule banning `scope=`, `headers=`, `request=request` kwargs to the structlog API in `src/`.
  3. Add a documented allowlist of safe kwarg shapes.
- **Recommended:** Option 2 (lint-time block) — cheaper than making the runtime walker heuristic. Document in CLAUDE.md / contributing guide.
- **Regression test:** Add `tests/core/test_logging.py::test_redactor_does_not_save_asgi_headers_shape` that asserts a list-of-tuples with `b"authorization"` in position 0 is **not** auto-redacted, then a second test asserting the lint rule (or Semgrep rule) blocks the offending pattern.

### SEV-4 — Forward-looking: regex over-redaction trap reproducible
- **Description:** The original BL5 SEV-3 (`token_sha256_prefix` rename) is recurrence-prone. Any new field whose name substring-matches `token|secret|auth(orization)|bearer|cookie|session|credential|password|jwt|apikey|privkey|signature` or short-tokens `pwd|pin|otp|mfa|tfa|sid|creds|salt|nonce` (between non-letter boundaries) is silently `<redacted>`. The regex docstring acknowledges this is the design (over-redaction preferred), but the developer trap remains — the `<redacted>` placeholder appears in tests and production logs identically, so the breakage isn't loud.
- **Affected code:** `src/orchestrator/core/logging.py:55-68` (the regex itself + lack of an "intentionally visible" allowlist).
- **Fix:** add an `_INTENTIONALLY_VISIBLE` allowlist (e.g. `frozenset({"rejection_fingerprint"})`) consulted before the regex; document the convention; add a unit test that the allowlist works AND a test that asserts every existing intentional-visible field passes the redactor unchanged.
- **Regression test:** `tests/core/test_logging.py::test_intentionally_visible_keys_pass_redactor` — set up a dict with each allowlist key and assert plaintext after pipeline. Augments existing redaction coverage.

### SEV-4 — Test gap: correlation_id not asserted on auth-rejected/body-cap log events
- **Description:** Existing tests assert correlation_id on `api.request.received` and `api.request.completed` (correlation-id middleware tests). Nothing asserts that `api.auth.rejected` or `api.body_size_cap_exceeded` log lines also carry the correlation_id from the surrounding request_context. Live REPL capture confirms it works today; lack of test means regression is invisible.
- **Affected code:** `tests/api/test_middleware_bearer_auth.py`, `tests/api/test_middleware_body_size_cap.py`.
- **Fix:** add tests that send a request with a known `X-Correlation-ID`, capture stdout, assert the rejection / body-cap event line contains that exact correlation_id.
- **Regression test:** `test_auth_rejected_event_includes_correlation_id`, `test_body_size_cap_event_includes_correlation_id`.

### SEV-4 — Test gap: rejection_fingerprint negative test (not in regex)
- **Description:** No test pins the property "the redactor does NOT match `rejection_fingerprint`". A future regex tweak adding `fingerprint` (e.g. someone adds biometric handling) would silently break the field. The current test only verifies the field appears on the rejection event today — not the negative regex assertion.
- **Affected code:** `tests/core/test_logging.py`.
- **Fix:** `def test_sensitive_key_re_does_not_match_rejection_fingerprint(): assert _SENSITIVE_KEY_RE.search("rejection_fingerprint") is None`. Pinning the contract makes any future regex change that breaks BL5 fail at unit-test time, not at integration.
- **Regression test:** as above. Trivial.

---

## Non-findings

- **rejection_fingerprint propagation: PASS.** Live-captured JSON line shows `"rejection_fingerprint": "b996d87d"` alongside `"correlation_id": "aaaa…"`. The BL5 closure rationale (rename to escape regex) is empirically correct.
- **No raw token / Authorization header value ever passes through any logger call in BL5.** Verified by reading every `_log.*` / `log.*` call site.
- **CORS, body-cap, and lifespan logging carry no sensitive kwargs.**
- **Multi-level dict redaction works** for the canonical `request.headers.authorization` shape.
- **Cycle handling works** (verified by reading the `seen` set logic in `_walk`).
- **Reserved-key collision protection works** (`_protect_reserved_keys` rescues user kwargs that collide with `correlation_id` etc. — not a BL5-specific concern but verified safe alongside).
- **No f-string credential interpolation in BL5.**
- **uvicorn default access log does not echo Authorization.**
- **structlog has no automatic header/scope extractor.**

End of audit.
