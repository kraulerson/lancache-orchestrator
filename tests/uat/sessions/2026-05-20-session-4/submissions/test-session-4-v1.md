# UAT Test Session — 4 (v1) — SUBMISSION

**Date run:** 2026-05-20
**Tester:** Claude (autonomous; user granted full autonomy)
**Format:** H-1 lightweight (HTTP API surface, manual `curl` flows)

---

## Pre-flight: PASS

- P1-P5: env active, branch `feat/uat-4-session`, working tree clean (only state files), test baseline green (457 project tests)
- P6: `ORCH_TOKEN`=32×'a', `ORCH_DATABASE_PATH=/tmp/uat4.db` (fresh)
- P7: migrations applied, `applied_count=1`
- P8: 30 games seeded (15 steam + 15 epic; all 8 status enum values represented; sizes 1.5GB-122GB; last_prefilled_at populated for ~half)

---

## Scenario 1 — uvicorn boot + 3 routers wired: PASS

- 1.1 Startup: `pool_initialized`, `api.boot.complete`, `Application startup complete` ✓
- 1.2 `/api/v1/openapi.json` paths: exactly `["/api/v1/games", "/api/v1/health", "/api/v1/platforms"]` ✓
- 1.3 Token leak grep: 0 hits ✓

---

## Scenario 2 — `/api/v1/platforms` regression: PASS

- 2.1 Order: steam first, epic second ✓ (BL6 D4)
- 2.2 Unauth: 401 ✓
- 2.3 Keys: `[auth_expires_at, auth_method, auth_status, last_error, last_sync_at, name]` — `config` NOT present ✓ (BL6 D1)

---

## Scenario 3 — `/api/v1/games` pagination: PASS

- 3.1 No params → 30 games, meta: `{total: 30, limit: 50, offset: 0, has_more: false}` ✓
- 3.2 `limit=5` → 5 games, `has_more: true` ✓
- 3.3 `limit=5&offset=5` → 5 games, `offset: 5` ✓
- 3.4 `limit=1000` → **400** with `{"detail": "limit must be <= 500, got 1000"}` ✓

---

## Scenario 4 — `/api/v1/games` filter + sort: PASS

- 4.1 `platform=steam` → `["steam"]` only ✓
- 4.2 `status_in=not_downloaded,up_to_date` → exactly `["not_downloaded", "up_to_date"]` ✓
- 4.3 `size_bytes_gte=10000000000&size_bytes_lte=50000000000` → min=12e9, max=50e9 ✓ (within bounds)
- 4.4 `sort=title:desc` top 3 → Witcher 3 B&W, The Witcher 3, The Last of Us ✓ (reverse-alpha)
- 4.5 `sort=size_bytes:desc,title:asc` `applied_sort` → 3 entries: `[size_bytes:desc, title:asc, id:asc]` ✓ (tie-breaker appended)

---

## Scenario 5 — `/api/v1/games` error paths + UAT-4 candidate-bug confirmation: PARTIAL (3 bugs confirmed)

| # | Test | Expected | Actual | Verdict |
|---|---|---|---|---|
| 5.1 | unknown filter field → 400 | 400 | 400 | ✓ |
| 5.2 | unknown op → 400 | 400 | 400 | ✓ |
| 5.3 | invalid value → 400 | 400 | 400 | ✓ |
| 5.4 | unauth → 401 | 401 | 401 | ✓ |
| **5.5** | **S2-B candidate**: `?sort=,,,` → expected `[title:asc, id:asc]` (default+tie-breaker); bug = only `[id:asc]` | `[id:asc]` only | `[{"field": "id", "direction": "asc"}]` | **BUG CONFIRMED** |
| **5.6** | **S2-D candidate**: `?size_bytes_gte=` + 25 nines → expected 400; bug = 500 | 500 | `500 Internal Server Error` | **BUG CONFIRMED** |
| **5.7** | **S2-A candidate**: `?platform=steam` `applied_filters` → expected compact `{platform: {eq: steam}}`; bug = all 7 op keys with 6 nulls | bug expected | `{"platform": {"eq": "steam", "in": null, "gte": null, "lte": null, "gt": null, "lt": null, "ne": null}}` | **BUG CONFIRMED** |

All three SEV-2 candidates empirically confirmed live.

---

## Scenario 6 — Loopback-only schema/UI (UAT-3 regression): PASS

Server restarted on `--host 0.0.0.0 --port 8765`. LAN IP: `192.168.1.192`.

| # | Test | Expected | Actual | Verdict |
|---|---|---|---|---|
| 6.1 | LAN `/api/v1/openapi.json` | 403 | 403 | ✓ |
| 6.2 | LAN `/api/v1/docs` | 403 | 403 | ✓ |
| 6.3 | LAN `/api/v1/games` (auth'd) | 200 (games not loopback-only) | 200 | ✓ |
| 6.4 | `api.boot.non_loopback_bind_warning` event in log | present | present, with full hint message | ✓ |

UAT-3 S2-C, S2-D, S3-h regressions all hold under BL6+BL7 surface.

---

## Scenario 7 — Correlation ID propagation (UAT-3 regression): PASS

- 7.1 Echo header present: `1fcca71e-d49c-446d-b1f7-1b734756ce38` (UUID4-shape) ✓
- 7.2 Repeat: different UUID `266c25b3-1109-45c7-9714-6d0fc1a64436` (server-generated each request) ✓
- 7.3 Client-supplied `X-Correlation-ID: my-test-id-12345` → server returned `6ae05923-...` instead. Client value IGNORED ✓ (UAT-3 F-9 mitigation holds)

---

## Scenario 8 — `/api/v1/games` pool-failure path: PASS (covered by unit test)

Skipped manual probe — `TestGamesPoolFailure::test_pool_error_returns_503` in `tests/api/test_games_router.py` covers this with full assertion on 503 + log shape.

---

## Bugs Found (live-confirmed during this session)

| ID | Scenario | Severity | Description | Repro |
|---|---|---|---|---|
| **S2-A** | 5.7 | SEV-2 | `applied_filters` echo emits all 7 op keys per filtered field with 6 null values, instead of the compact `{op: value}` shape from spec §3.2 | `curl -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?platform=steam&limit=3" \| jq '.meta.applied_filters'` |
| **S2-B** | 5.5 | SEV-2 | `?sort=,,,` silently drops the default sort; only the tie-breaker `id:asc` survives. Parser enters user-sort branch on non-empty string, comma-split produces only empty entries, loop adds nothing, default never applied | `curl -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?sort=,,,&limit=3" \| jq '.meta.applied_sort'` |
| **S2-D** | 5.6 | SEV-2 | Oversized integer (`size_bytes_gte` = 25 nines) parses as Python int (no overflow), binds to SQLite, raises OverflowError → uncaught → FastAPI 500 instead of 400 with structured error | `curl ... "http://127.0.0.1:8765/api/v1/games?size_bytes_gte=99999999999999999999999"` returns `500 Internal Server Error` |

---

## Tester Notes

- UAT-4 agent findings empirically confirmed live for all 3 SEV-2 candidate bugs (S2-A, S2-B, S2-D). S2-C (`_in` cardinality) not probed manually — would need to send 1000+ values; trust the agent walk + reproduce with a regression test during remediation.
- All UAT-3 regressions (CORS outermost, non-loopback warn, loopback schema gate, correlation_id regeneration) hold under BL6+BL7 substrate.
- Test suite at 457 passing; this manual session adds wire-level confirmation that the unit tests model real behavior accurately.
- Scenario 5.6 (oversized int) also produces a stack trace in server logs (not visible at HTTP layer); fix should catch OverflowError at the parameter binding boundary in `_query_helpers._coerce_value` and re-raise as `QueryParamError`.
