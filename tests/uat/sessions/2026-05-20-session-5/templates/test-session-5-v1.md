# UAT Test Session — 5 (v1)

**Date:** 2026-05-20
**Features Under Test:** BL8 (`/api/v1/jobs`) + BL9 (`/api/v1/manifests` + `?include=` convention)
**Tester:** Assistant (per UAT-4 precedent, assistant runs manual session)
**Format:** H-1 lightweight (HTTP API surface, manual `curl` flows)

Counter at 2/2 — UAT-5 required before BL10.

---

## Pre-flight

| # | Check | Command | Expected |
|---|---|---|---|
| P1 | venv active | `which python` | `…/lancache_orchestrator/.venv/bin/python` |
| P2 | branch | `git branch --show-current` | `feat/uat-5-session` |
| P3 | clean tree | `git status --short` | (empty or only `M .claude/process-state.json`) |
| P4 | full suite green | `pytest -q` | 560 pass |
| P5 | configure test token + DB | `export ORCH_TOKEN=$(printf 'a%.0s' {1..32}); export ORCH_DATABASE_PATH=/tmp/uat5.db; rm -f $ORCH_DATABASE_PATH` | (no output) |
| P6 | seed migration | `python -m orchestrator.db.migrate "$ORCH_DATABASE_PATH"` | exit 0 |
| P7 | seed game + manifest data | sqlite3 inserts (see Appendix A) | inserts 5 games + 21 manifests + 2 jobs |

Pre-flight all-pass: ☐

---

## Scenario 1 — Server boot, all 5 routers wired

```
uvicorn orchestrator.api.main:app --host 127.0.0.1 --port 8765 --log-level info
```

| Step | Check | Expected |
|---|---|---|
| 1.1 | startup logs | `pool_initialized` + Application startup complete |
| 1.2 | OpenAPI paths | `curl -s http://127.0.0.1:8765/api/v1/openapi.json | jq '.paths | keys'` → contains `/api/v1/health`, `/api/v1/platforms`, `/api/v1/games`, `/api/v1/jobs`, `/api/v1/manifests` |
| 1.3 | no token leak in startup | `0` matches for the 32-char token literal in stdout |
| 1.4 | docs UI loopback-restricted | LAN `/api/v1/docs` → 403; loopback → 200 |

---

## Scenario 2 — `/api/v1/jobs` happy path + filter

T=$ORCH_TOKEN. The seed has 2 jobs from the populated_pool fixture analog.

| Step | Command | Expected |
|---|---|---|
| 2.1 default list | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/jobs" | jq '.jobs | length, .meta'` | array length, meta with total/limit/offset/has_more/applied_*  |
| 2.2 state filter | `…/jobs?state=succeeded | jq '[.jobs[].state] | unique'` | `["succeeded"]` only |
| 2.3 kind filter | `…/jobs?kind=manifest_fetch` | filtered subset |
| 2.4 applied_filters compact | `…/jobs?state=succeeded | jq '.meta.applied_filters'` | `{"state": {"eq": "succeeded"}}` (UAT-4 S2-A regression) |
| 2.5 unknown filter | `…/jobs?password=foo -o /dev/null -w '%{http_code}'` | `400` |
| 2.6 unauth | `…/jobs (no auth)` | `401` |
| 2.7 default sort id:desc | first `id` should be highest |

---

## Scenario 3 — `/api/v1/manifests` happy path + filter + sort

Seed has 21 manifests across 5 games.

| Step | Command | Expected |
|---|---|---|
| 3.1 default list | `…/manifests | jq '.meta.total, .meta.applied_sort'` | total=21, applied_sort=[{fetched_at:desc},{id:asc}] |
| 3.2 default sort | inspect first 3 `fetched_at` values descending |
| 3.3 game_id filter | `…/manifests?game_id=1&limit=500` | all rows game_id=1 |
| 3.4 game_id_in | `…/manifests?game_id_in=1,2&limit=500` | all rows in {1,2} |
| 3.5 chunk_count range | `…/manifests?chunk_count_gte=1000&chunk_count_lte=5000` | all in range |
| 3.6 fetched_at range | `…/manifests?fetched_at_gte=2026-05-15T00:00:00Z` | all timestamps >= cutoff |
| 3.7 sort by total_bytes desc | first row has the largest total_bytes (100 GB) |
| 3.8 raw BLOB excluded | `…/manifests | jq '.manifests[0] | keys'` | NO `raw` key |
| 3.9 applied_filters echo | `…/manifests?game_id=1&chunk_count_gte=1000 | jq '.meta.applied_filters'` | `{"game_id":{"eq":1},"chunk_count":{"gte":1000}}` |

---

## Scenario 4 — `?include=game` BL9 convention

| Step | Command | Expected |
|---|---|---|
| 4.1 no include = null | `…/manifests?limit=3 | jq '.manifests[].game'` | three `null` |
| 4.2 include=game populated | `…/manifests?include=game&limit=3 | jq '.manifests[].game | keys'` | `["app_id","platform","title"]` per row |
| 4.3 cross-row game data matches | `…/manifests?game_id=1&include=game&limit=3 | jq '.manifests[].game'` | all show Counter-Strike (steam, app_id=10) |
| 4.4 applied_includes echo | `…/manifests?include=game | jq '.meta.applied_includes'` | `["game"]` |
| 4.5 dedup | `…/manifests?include=game,game,game | jq '.meta.applied_includes'` | `["game"]` (single entry) |
| 4.6 unknown key 400 | `…/manifests?include=games -o /dev/null -w '%{http_code}'` | `400`, body contains "include keys not allowed" |
| 4.7 empty include | `…/manifests?include= | jq '.meta.applied_includes'` | `[]` |
| 4.8 with filter combo | `…/manifests?game_id_in=1,2&include=game&limit=10` | rows correctly filtered + game inline |

---

## Scenario 5 — UAT-4 regressions still hardened (per memory)

| Step | Command | Expected |
|---|---|---|
| 5.1 S2-A jobs applied_filters compact | `…/jobs?state=succeeded&limit=3 | jq '.meta.applied_filters'` | exactly `{"state":{"eq":"succeeded"}}` (no 6 null keys) |
| 5.2 S2-A manifests applied_filters compact | `…/manifests?game_id=1&limit=3 | jq '.meta.applied_filters'` | `{"game_id":{"eq":1}}` |
| 5.3 S2-B sort=,,, applies default | `…/manifests?sort=,,,&limit=3 | jq '.meta.applied_sort'` | `[{fetched_at:desc},{id:asc}]` (default + tie-breaker, NOT just tie-breaker) |
| 5.4 S2-C _in cap 100 | `…/manifests?game_id_in=$(python -c "print(','.join('1' for _ in range(101)))") -o /dev/null -w '%{http_code}'` | `400` |
| 5.5 S2-D INT64 overflow | `…/manifests?total_bytes_gte=99999999999999999999999 -o /dev/null -w '%{http_code}'` | `400` (NOT 500) |
| 5.6 S3-a XSS in timestamp | `…/manifests?fetched_at_gte=<script>alert(1)</script> -o /dev/null -w '%{http_code}'` | `400` |

---

## Scenario 6 — Cross-router consistency (BL6 / 7 / 8 / 9)

| Step | Test | Expected |
|---|---|---|
| 6.1 401 on all without auth | `/platforms`, `/games`, `/jobs`, `/manifests` | all `401` |
| 6.2 400 on unknown filter on all | `?password=foo` against each | all `400` |
| 6.3 envelope keys consistent | `keys` of each response | `["{entity}", "meta"]` exactly |
| 6.4 applied_filters compact shape on all | same `{field:{op:val}}` shape | consistent across endpoints |
| 6.5 service-tag log namespace | grep `api.{entity}.read_failed` event names in code | one per router (platforms, games, jobs, manifests) |

---

## Scenario 7 — UAT-3 regressions

| Step | Test | Expected |
|---|---|---|
| 7.1 CORS outermost | OPTIONS preflight against `/manifests` from disallowed Origin → 400 or no CORS headers; allowed Origin → 200 with CORS headers |
| 7.2 Correlation ID server-generated | `curl -sI -H "Authorization: Bearer $T" -H "X-Correlation-ID: my-test-id" .../manifests | grep -i x-correlation-id` → server UUID, NOT `my-test-id` |
| 7.3 Body cap on these GETs | Send a 10MB body with GET; expect ignored / no error |
| 7.4 Loopback enforcement | `/api/v1/openapi.json` accessible only on 127.0.0.1 |

---

## Bugs Found

| ID | Scenario | Severity | Description | Repro |
|---|---|---|---|---|
| | | | | |

---

## Cleanup

```
# Ctrl-C uvicorn
rm -f /tmp/uat5.db
unset ORCH_TOKEN ORCH_DATABASE_PATH
```

---

## Appendix A — UAT-5 seed SQL (5 games + 21 manifests + 2 jobs)

```sql
INSERT INTO platforms (name, cache_identifier, slice_bytes, levels, last_known_status, last_error)
VALUES ('steam','steam',10485760,'2:2','healthy',NULL),
       ('epic','epicgames',10485760,'2:2','healthy',NULL);

INSERT INTO games (id, platform, app_id, title, owned, size_bytes, status, last_prefilled_at, metadata)
VALUES
  (1,'steam','10','Counter-Strike',1,5000000000,'up_to_date','2026-05-15T00:00:00Z','{"depots":[101]}'),
  (2,'steam','440','Team Fortress 2',1,25000000000,'up_to_date','2026-05-10T00:00:00Z','{"depots":[441]}'),
  (3,'steam','570','Dota 2',1,75000000000,'pending_update','2026-05-08T00:00:00Z','{"depots":[571]}'),
  (4,'epic','fortnite','Fortnite',1,30000000000,'up_to_date','2026-05-16T00:00:00Z','{"build_version":"++Fortnite+Release-30.20"}'),
  (5,'epic','rocketleague','Rocket League',1,25000000000,'up_to_date','2026-05-14T00:00:00Z','{"build_version":"v2.40"}');

-- 21 manifests across 5 games (mix of sizes, fetched_at spread across past month)
INSERT INTO manifests (game_id, version, fetched_at, chunk_count, total_bytes, raw)
VALUES
  (1,'10001','2026-04-22T12:00:00Z',100,1000000000,X'28b52ffd000073747562'),
  (1,'10002','2026-04-29T12:00:00Z',250,2500000000,X'28b52ffd000073747562'),
  (1,'10003','2026-05-06T12:00:00Z',1820,5000000000,X'28b52ffd000073747562'),
  (1,'10004','2026-05-13T12:00:00Z',5000,25000000000,X'28b52ffd000073747562'),
  (1,'10005','2026-05-19T12:00:00Z',12000,75000000000,X'28b52ffd000073747562'),
  (2,'20001','2026-04-25T12:00:00Z',500,5000000000,X'28b52ffd000073747562'),
  (2,'20002','2026-05-10T12:00:00Z',1500,15000000000,X'28b52ffd000073747562'),
  (2,'20003','2026-05-17T12:00:00Z',3000,30000000000,X'28b52ffd000073747562'),
  (3,'30001','2026-04-20T12:00:00Z',100,500000000,X'28b52ffd000073747562'),
  (3,'30002','2026-04-30T12:00:00Z',800,8000000000,X'28b52ffd000073747562'),
  (3,'30003','2026-05-08T12:00:00Z',1200,12000000000,X'28b52ffd000073747562'),
  (3,'30004','2026-05-15T12:00:00Z',2400,22000000000,X'28b52ffd000073747562'),
  (4,'v1.0.0','2026-04-23T12:00:00Z',200,1500000000,X'28b52ffd000073747562'),
  (4,'v1.1.0','2026-05-01T12:00:00Z',450,4500000000,X'28b52ffd000073747562'),
  (4,'v1.2.0','2026-05-09T12:00:00Z',900,9000000000,X'28b52ffd000073747562'),
  (4,'v2.0.0','2026-05-16T12:00:00Z',2200,22000000000,X'28b52ffd000073747562'),
  (5,'++Release-1.0','2026-04-21T12:00:00Z',350,3500000000,X'28b52ffd000073747562'),
  (5,'++Release-1.1','2026-04-28T12:00:00Z',700,7000000000,X'28b52ffd000073747562'),
  (5,'++Release-1.2','2026-05-05T12:00:00Z',1400,14000000000,X'28b52ffd000073747562'),
  (5,'++Release-2.0','2026-05-12T12:00:00Z',2800,28000000000,X'28b52ffd000073747562'),
  (5,'++Release-2.1','2026-05-18T12:00:00Z',50000,100000000000,X'28b52ffd000073747562');

INSERT INTO jobs (kind, game_id, platform, state, source, started_at, finished_at, payload, last_error, progress)
VALUES
  ('prefill', 1, 'steam', 'succeeded', 'scheduler', '2026-04-04T00:00:00Z', '2026-04-04T00:05:00Z', '{"depot":101}', NULL, 100),
  ('sweep', NULL, NULL, 'succeeded', 'scheduler', '2026-04-04T00:00:00Z', '2026-04-04T00:05:00Z', '[1,2,3]', NULL, 100);
```
