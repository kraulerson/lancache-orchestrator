# UAT-5 Manual Session — Submission v1

**Tester:** Assistant (per UAT-4 precedent)
**Date:** 2026-05-20
**Branch:** `feat/uat-5-session`
**DB:** `/tmp/uat5.db` (5 games + 21 manifests + 2 jobs + 2 platforms)
**Server:** `uvicorn orchestrator.api.main:app --host 127.0.0.1 --port 8765`

---

## Pre-flight

| # | Check | Result |
|---|---|---|
| P1 | venv active | PASS |
| P2 | branch | PASS (`feat/uat-5-session`) |
| P3 | clean tree | PASS |
| P4 | pytest -q | PASS (560 tests) |
| P5 | env vars | PASS |
| P6 | migration | PASS (applied_count=1) |
| P7 | seed data | PASS (after schema fix — see Tester Notes) |

---

## Scenario 1 — Server Boot + OpenAPI

| # | Result | Evidence |
|---|---|---|
| 1.1 | PASS | uvicorn boots cleanly; pool_initialized + application startup logged |
| 1.2 | PASS | OpenAPI paths = `['/api/v1/games', '/api/v1/health', '/api/v1/jobs', '/api/v1/manifests', '/api/v1/platforms']` — all 5 routers wired |
| 1.3 | PASS | 0 matches for 32-char token literal in server stdout |

---

## Scenario 2 — `/api/v1/jobs` happy path

| # | Result | Evidence |
|---|---|---|
| 2.1 | PASS | total=2, limit=50, has_more=false, default sort `[{field:id, direction:desc}]` |
| 2.2 | PASS | `?state=succeeded` → only `succeeded` rows returned |
| 2.4 | PASS | applied_filters = `{"state":{"eq":"succeeded"}}` — compact shape (UAT-4 S2-A regression hardened) |
| 2.5 | PASS | `?password=foo` → 400 |
| 2.6 | PASS | unauth → 401 |

---

## Scenario 3 — `/api/v1/manifests` happy path

| # | Result | Evidence |
|---|---|---|
| 3.1 | PASS | total=21, applied_sort=`[{field:fetched_at,direction:desc},{field:id,direction:asc}]` |
| 3.2 | PASS | first 3 fetched_at desc: `['2026-05-19T12:00:00Z', '2026-05-18T12:00:00Z', '2026-05-17T12:00:00Z']` |
| 3.3 | PASS | `?game_id=1` → all rows game_id=1 |
| 3.4 | PASS | `?game_id_in=1,2` → only {1,2} |
| 3.5 | PASS | chunk_count range 1000-5000 → 9 rows, min=1200, max=5000 |
| 3.6 | PASS | fetched_at_gte 2026-05-15 → 5 rows all >= cutoff |
| 3.7 | PASS | sort=total_bytes:desc → first row 100,000,000,000 (100 GB ++Release-2.1) |
| 3.8 | PASS | response keys = `['chunk_count', 'fetched_at', 'game', 'game_id', 'id', 'total_bytes', 'version']` — **NO `raw` key** (D1 enforced) |
| 3.9 | PASS | applied_filters = `{"game_id":{"eq":1}, "chunk_count":{"gte":1000}}` |

---

## Scenario 4 — `?include=game` (BL9 convention)

| # | Result | Evidence |
|---|---|---|
| 4.1 | PASS | no include → all `game` = `None` |
| 4.2 | PASS | include=game → game object with keys `['app_id', 'platform', 'title']` |
| 4.3 | PASS | game_id=1 + include=game → all show `(('app_id', '10'), ('platform', 'steam'), ('title', 'Counter-Strike'))` |
| 4.4 | PASS | applied_includes = `["game"]` |
| 4.5 | PASS | include=game,game,game → applied_includes = `["game"]` (deduped) |
| 4.6 | PASS | include=games (typo) → 400 with `detail="include keys not allowed: ['games']"` |
| 4.7 | PASS | include= (empty) → applied_includes = `[]` |
| 4.8 | PASS | game_id_in=1,4 + include=game → titles = `{'Counter-Strike', 'Fortnite'}` |

---

## Scenario 5 — UAT-4 regressions still hardened

| # | Result | Evidence |
|---|---|---|
| 5.1 | PASS | jobs applied_filters compact: `{"state":{"eq":"succeeded"}}` (no 6 null keys) |
| 5.2 | PASS | manifests applied_filters compact: `{"game_id":{"eq":1}}` |
| 5.3 | PASS | sort=,,, → `[{fetched_at:desc},{id:asc}]` default still applied (S2-B fix holds) |
| 5.4 | PASS | _in cap 101 values → 400 (S2-C cap holds at 100) |
| 5.5 | PASS | INT64 overflow → 400 with descriptive detail (S2-D fix holds) |
| 5.6 | PASS | `<script>alert(1)</script>` in fetched_at_gte → 400 with descriptive detail (S3-a fix holds) |

---

## Scenario 6 — Cross-router consistency

| # | Result | Evidence |
|---|---|---|
| 6.1 | PASS | All 4 endpoints return 401 on missing auth |
| 6.2 | **FAIL — Finding M1** | `/api/v1/platforms?password=foo` → **200** (other 3 endpoints → 400). Platforms silently ignores unknown query params. |
| 6.3 | **FAIL — Finding M2** | `/api/v1/platforms` envelope = `{platforms}` (no `meta` key). Other 3 endpoints have `{entity, meta}`. |

---

## Scenario 7 — UAT-3 regressions

| # | Result | Evidence |
|---|---|---|
| 7.1 | **FAIL — Finding M3** | OPTIONS preflight returns 400 (both with and without Origin in allow-list, both with and without valid auth header). CORS-relevant headers ARE attached, but the status is wrong. Expected 200/204 for preflight. |
| 7.2 | PASS | server-regenerates correlation ID (sent `X-Correlation-ID: my-test-id`, got `38029ebb-fd24-...`) |
| 7.3 | PASS | 10MB body on GET → 413 from BodySizeCapMiddleware (cap=32768) |
| 7.4 | PASS | LAN IP request to localhost-bound uvicorn → connection refused (correct loopback enforcement) |

---

## Bugs Found (manual session only)

| ID | Scenario | Severity | Description | Repro |
|---|---|---|---|---|
| M1 | 6.2 | SEV-3 | `/api/v1/platforms` accepts unknown query params with 200 instead of 400 | `curl /api/v1/platforms?password=foo` → 200 (others → 400). Root cause: platforms router doesn't use `_query_helpers`/`FilterAllowList` |
| M2 | 6.3 | SEV-3 | `/api/v1/platforms` envelope shape lacks `meta` key — inconsistent with games/jobs/manifests | response is `{platforms:[…]}` only, missing `meta` envelope. May be by-design (no pagination needed) but undocumented |
| M3 | 7.1 | SEV-3 | OPTIONS preflight returns 400 instead of 200/204 | `curl -X OPTIONS -H "Origin: …" /api/v1/manifests` → 400 even with valid auth. CORS browsers would interpret this as preflight failure → can't load the API from a browser |

Note: M1 and M2 confirmed by Agent 3 cross-feature audit as F1 and F2.

---

## Tester Notes

- **Schema mismatch in initial seed SQL**: my template's Appendix A used columns `cache_identifier`, `slice_bytes`, `levels`, `last_known_status` for platforms and `last_error`, integer `progress` for jobs. Actual schema has `auth_status`, `auth_method`, `auth_expires_at`, `last_sync_at`, `last_error`, `config` for platforms; jobs uses `error` (not `last_error`) and `progress REAL 0.0-1.0`. Memory `[[lancache-deployment-params]]` refers to slice/levels values that are NOT on the platforms table — they're operational config values from `/lancache/.env`, not schema fields. Worth noting for future UAT template generation to source from the actual migration file.
- Body cap = 32768 (32 KB) default. This is fine for GET payloads but if Game_shelf needs to POST larger payloads later (config blobs, batch update lists), needs to be sized up.
- OPTIONS preflight behavior (M3) is potentially load-bearing for a browser client. Worth investigating root cause in remediation. Could be a middleware ordering issue or an OPTIONS handler that's not registered for these endpoints.
- All scenarios I planned were exercisable — no blockers, server cleanly handled probing.

---

## Cleanup

```bash
pkill -f "uvicorn orchestrator.api.main"
rm -f /tmp/uat5.db /tmp/uat5-server.log
unset ORCH_TOKEN ORCH_DATABASE_PATH
```
