# UAT-5 Agent 2 — Exploratory Tester / Malicious User

**Date:** 2026-05-20
**Scope:** BL5 (/health), BL6 (/platforms), BL7 (/games), BL8 (/jobs), BL9 (/manifests)
**Method:** 93 pytest probes against `unit_app` via ASGITransport + seeded pool fixtures.
Token: `"a" * 32`. Probe file: `probes.py` (sibling). Each probe emits a structured
`UAT5_FINDING:` JSON line under `pytest -s`.

**Summary:** 91 / 93 probes asserted clean. **2 confirmed bugs** (same root
cause). **5 documented behavior smells** worth orchestrator triage. Hardening
is broadly excellent — auth, body cap, identifier validation, sort/include
parsing, INT64 / IN-cap defenses, and OQ2 loopback are all working as
designed under hostile inputs.

---

## CONFIRMED BUGS

### BUG-1 — SEV-2: Unhandled `ValueError` on `NaN` / `Infinity` in float filter (jobs router)

**Endpoint:** `GET /api/v1/jobs?progress_gte=NaN` (and `progress_gte=Infinity`,
`progress_lte=-Infinity`).

**Reproducer (probe IDs X8, X9):**
```http
GET /api/v1/jobs?progress_gte=NaN HTTP/1.1
Authorization: Bearer aaaa...aaaa (32x)
```

**Observed:**
- `_coerce_value` calls `float("NaN")` → returns `nan` (Python accepts it).
- `parse_filters` records `{"progress": {"gte": nan}}` and passes it on.
- SQL executes with `progress >= ?` and param `nan`. SQLite returns no rows.
  Total = 0; manifests = []. No DB-level explosion.
- Endpoint builds `JobListResponse(meta=JobsMeta(applied_filters={"progress":{"gte": nan}}))`.
- `JSONResponse(content=body.model_dump(...))` runs Python stdlib `json.dumps` on
  the dict. `json.dumps` raises:
  ```
  ValueError: Out of range float values are not JSON compliant: nan
  ```
- ASGI exception escapes the handler. Starlette's exception middleware would
  return a 500 with a generic message in prod; in tests the exception bubbles
  to the client (`httpx` re-raises). The handler does NOT catch this.

**Expected:** 400 at parse time — reject non-finite floats before they enter
the response envelope.

**Severity:** SEV-2 (DoS-grade — any unauthenticated client with a valid bearer
token can pin the request to 500. Pollutes logs with full traceback. Easy to
script.)

**Surface:**
- `progress_gte`, `progress_lte` on `/api/v1/jobs` (only float-typed filter
  field on any read endpoint today).
- Re-occurs the moment **any** future endpoint adds a `float` filter via
  `FilterFieldSpec(value_type=float, ...)`.

**Suggested fix:** in `_query_helpers._coerce_value`, after `float(raw)`,
add `if not math.isfinite(coerced): raise ValueError("non-finite float not allowed")`.
Single-line defense, exactly mirroring the int-range check already on line 236.
This covers `nan`, `+inf`, `-inf`, and any locale-spelled variant Python's
`float()` accepts.

**Recommended test additions:**
- `test_jobs_progress_nan_rejected_400` (existing failing probe X8)
- `test_jobs_progress_inf_rejected_400` (existing failing probe X9)
- `test_jobs_progress_neg_inf_rejected_400` (new — `progress_gte=-Infinity`)
- `test_jobs_progress_finite_ok` (sanity)

---

## BEHAVIOR SMELLS (orchestrator triage required)

### SMELL-1 — SEV-3: Duplicate-field+different-direction sort silently emitted

**Probe S6:** `GET /api/v1/manifests?sort=id:asc,id:desc`
**Observed:** 200, `applied_sort = [{"field":"id","direction":"asc"}, {"field":"id","direction":"desc"}]`.
SQL emitted is `ORDER BY id ASC, id DESC` — wasted clause, second is ignored.
Tie-breaker dedupe code drops the auto-appended `id:asc` (correct), but
doesn't notice user-supplied duplicates.

**Risk:** Low. SQLite tolerates it; result is well-defined (first sort wins).
But the meta echo is misleading and an attacker can multiply the ORDER BY
to ~100 entries (probe R9 confirmed). Not a DoS at current limits, but
worth deduplicating in `parse_sort` for cleanliness.

**Suggested fix:** in `parse_sort`, after appending each user entry, check
if the field already appears in `user_sort`; if so, skip (first occurrence
wins). Tests already cover the tie-breaker dedupe path — extend with a
user-side dedupe case.

### SMELL-2 — SEV-4: `eq` + `_in` on the same field produces always-empty AND

**Probe O4:** `GET /api/v1/manifests?game_id=1&game_id_in=2,3`
**Observed:** 200, total=0, `applied_filters = {"game_id": {"eq": 1, "in": [2, 3]}}`.
SQL is `game_id = ? AND game_id IN (?, ?)` — only matches game_id = 1 AND
game_id IN (2,3) = never.

**Risk:** Cosmetic. Caller gets the right "no matches" answer for their
contradictory query. But silently returning 0 rows for what's clearly a
caller bug is unfriendly. Either reject (400) or document that combining
eq with _in is an AND.

### SMELL-3 — SEV-4: `?include=` silently ignored on endpoints without expansion

**Probes I8, I9:** `GET /api/v1/games?include=foo` and `/api/v1/jobs?include=foo` both return 200, ignore the param.

Reason: `"include"` is in `_RESERVED_PARAM_NAMES` so `parse_filters` skips it,
and games/jobs never call `parse_includes`.

**Risk:** UX only. A caller asking for an include on an endpoint that doesn't
support any expansion gets no signal. Could mask client-side typos
("include=games" vs "include=game" — first 200s, second 400s, on the same
codebase).

**Suggested fix:** option (a) make every router call `parse_includes` with an
empty `IncludeAllowList({})` so unknown includes 400; (b) document that
`?include=` on games/jobs is silently ignored; (c) hoist the include-parse
step into a tiny shared helper that's always called.

### SMELL-4 — SEV-4: `int("1_000")` and `int("+1")` accepted by filter coercion

**Probes E6, E7:** `GET /api/v1/manifests?game_id=%2B1` → 200, `applied_filters: {"game_id":{"eq":1}}`.
`?game_id=1_000` → 200, `applied_filters: {"game_id":{"eq":1000}}`.

Python's `int()` is lenient about leading `+`, leading whitespace, and PEP 515
underscore separators. Filter param values inherit that leniency. Currently
all values bind through `?` parameter placeholders, so this is **not** an SQLi
vector. But the applied-filters echo confirms the server normalised the user's
value silently — caller cannot tell from the response whether their original
input round-trips.

**Risk:** Cosmetic. Tighten value parsing if strictness matters more than
forgiveness. I would NOT change this unless the orchestrator explicitly wants
strict parse — it would break valid-looking clients.

### SMELL-5 — SEV-4: `/api/v1/health` returns HTTP 503 but body `status: "ok"`

**Probe HL1:** No auth, default config. `pool_ok=True`, BL5 stubs all False
(scheduler_running/lancache_reachable/validator_healthy). Endpoint computes
`status="ok"` (pool-only) and `all_healthy=False` (full stack) → returns
HTTP 503 with body `{"status":"ok", ...}`.

**Observed body:**
```json
{"status":"ok","version":"0.1.0","uptime_sec":0,"scheduler_running":false,
 "lancache_reachable":false,"cache_volume_mounted":false,
 "validator_healthy":false,"git_sha":"test-sha"}
```

**Risk:** Operator confusion. A `/health` returning 503 with `status:"ok"`
is contradictory on the wire. Acceptable today because BL5 deliberately
stubs the non-pool subsystems and the test stays under the radar — but as
soon as those stubs become real, the response will keep saying `status:"ok"`
even when the orchestrator is degraded.

**Suggested fix:** compute `status` from the same logical conjunction as the
HTTP code, or rename the body field (`pool_status`/`db_status`) so the two
are not visibly conflated. Either way is fine; consistency matters more
than the choice.

---

## INTERESTING-BUT-NOT-A-BUG OBSERVATIONS

| ID | Endpoint | Probe | Observation |
|---|---|---|---|
| A8 | manifests | `Bearer   tok   ` (extra spaces) | 200. Token stripped — middleware does `.strip()` after `find(" ")`. RFC-compatible; flag-only. |
| A9 | manifests | Two `Authorization:` headers (wrong, valid) | 200. ASGI dedupes — last value wins. RFC behavior. |
| C1 | manifests | 1000-char `X-Correlation-ID` | Server regenerates UUID4 (UAT-3 fix confirmed). Echoed value is 36 chars, not the 1000-char input. |
| C2 | manifests | `X-Correlation-ID: abc\r\nX-Injected: yes` | 200. httpx sanitizes at client side; server never sees the CRLF. Good — header injection blocked at transport. |
| H1/H2 | docs | `X-Forwarded-For: 127.0.0.1` from external client | 403. OQ2 reads `scope["client"][0]` directly, ignores XFF. Confirmed. |
| H4 | manifests | `Origin: http://evil.example.com` | 200, no ACAO header (`allow_origins=[]` by default). CORS misconfig would silently expose data — current default is safe. |
| B1 | manifests | 64 KiB body on GET | 413. BodySizeCap works on any method; body-cap fires before route dispatch. |
| R1 | manifests | `offset=9_223_372_036_854_775_806` (just under INT64_MAX) | 200 empty. INT64 boundary correctly inclusive. |
| R6 | manifests | `game_id_in=1,2,...,100` (exactly cap) | 200. MAX_IN_VALUES = 100 inclusive. |
| R7 | manifests | `game_id_in=1,...,101` (one over cap) | 400. UAT-4 S2-C enforced. |
| HL3 | healthxxx | substring of `/api/v1/health` | 401. UAT-3 S2-A regression NOT re-introduced. |
| I4/I5 | manifests | `include=Game` (uppercase) | 400. Identifier-validated allow-list rejects case-mismatch. |
| O1 | manifests | `game_id_ne=1` | 400. Operator allow-list per field works. |
| O3 | manifests | `chunk_count_in=...` | 400. `in` op not allowed for chunk_count. |
| S5 | manifests | `sort=%20,%20,%20,%20` (all whitespace) | 200, default sort applied. UAT-4 S2-B fix in place. |
| X3 | games | `progress_gte=0.5` (jobs-only field) | 400 "unknown filter field". Field-name allow-list per endpoint correctly partitions. |

---

## METHODOLOGY NOTES

- Ran via `PYTHONPATH=src .venv/bin/pytest tests/uat/sessions/2026-05-20-session-5/agent-results/probes.py -v -s`.
- 91 PASS / 2 FAIL. Both failures are the SEV-2 bug (NaN + Infinity, same code path).
- Fixtures: `client` (ASGITransport, no socket), `external_client` (192.168.1.100
  client tuple — OQ2 spoof tests), `manifests_pool_seeded`, `jobs_pool_seeded`,
  `games_pool_100`. All from `tests/api/conftest.py` (re-imported in probes.py).
- One non-finding: my initial pagination tests assumed 21 manifests (from
  `manifests_pool_seeded` alone) but `populated_pool` already inserts 3
  baseline manifests, so actual total is 24. Updated probes; expectations
  now correct.

## RECOMMENDATIONS — ORDERED

1. **Land BUG-1 fix** in `_query_helpers._coerce_value` (non-finite float
   reject). One-line + two probe tests. SEV-2 deserves Fix-Now.
2. **Add `math.isfinite` defense + tests** as a permanent regression gate.
3. Triage SMELL-5 with the orchestrator — health body/status mismatch
   becomes user-visible the moment real subsystems land.
4. Optional cleanups: dedupe user-supplied sort fields (SMELL-1), decide
   on include-on-non-include-endpoint behavior (SMELL-3).
5. Document that `int()` leniency (`+`, `_`) is intentional, or tighten
   if strictness wins (SMELL-4). No security impact either way.

**Files relevant to this audit:**
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/_query_helpers.py` (line 229–257 — `_coerce_value`)
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/jobs.py`
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/src/orchestrator/api/routers/health.py` (line 60–89 — status vs all_healthy split)
- `/Users/karl/Documents/Claude Projects/lancache_orchestrator/tests/uat/sessions/2026-05-20-session-5/agent-results/probes.py` (this audit's probes)
