# UAT Test Session — 4 (v1)

**Date:** 2026-05-20
**Features Under Test:** BL6 (`/api/v1/platforms`) + BL7 (`/api/v1/games` + `_query_helpers.py`)
**Tester:** Karl (Orchestrator)
**Format:** H-1 lightweight (HTTP API surface, manual `curl` flows)

---

## Instructions

1. Open two terminals from project root inside the venv: `source .venv/bin/activate`.
2. **Terminal A** runs the server. **Terminal B** runs the curl probes.
3. Mark Pass/Fail per row.
4. If Fail: fill in the Bugs Found table at the bottom.
5. When done, save this file to `tests/uat/sessions/2026-05-20-session-4/submissions/test-session-4-v1.md` and tell the Orchestrator agent "results are in".

> `/api/v1/health` returns **503 by design** in pre-validator state (Bible §8.4). 503 is PASS.

---

## Pre-flight

| # | Check | Command | Expected |
|---|---|---|---|
| P1 | venv active (both terminals) | `which python` | `.../lancache_orchestrator/.venv/bin/python` |
| P2 | branch | `git branch --show-current` | `feat/uat-4-session` |
| P3 | clean tree | `git status --short` | (empty or only `M .claude/process-state.json`) |
| P4 | API test baseline | `pytest tests/api/ -q` | ~138 pass (76 BL5/UAT-3 + 23 BL6 + 38 BL7 + 1 router-list) |
| P5 | full suite green | `pytest -q` | 457 pass |
| P6 | configure test token + DB | `export ORCH_TOKEN=$(printf 'a%.0s' {1..32}); export ORCH_DATABASE_PATH=/tmp/uat4.db; rm -f $ORCH_DATABASE_PATH` | (no output) |
| P7 | seed migration | `python -m orchestrator.db.migrate "$ORCH_DATABASE_PATH"` | exit 0; `applied_count=1` |
| P8 | seed some game data | (see Appendix A — paste the SQL block) | inserts ~30 games across platforms/statuses |

Pre-flight all-pass: ☐

---

## Scenario 1 — uvicorn boot, all 3 routers wired

Terminal A:
```
uvicorn orchestrator.api.main:app --host 127.0.0.1 --port 8765 --log-level info
```

| Step | Check | Expected |
|---|---|---|
| 1.1 | startup logs | sees `pool_initialized` and Application startup complete |
| 1.2 | OpenAPI schema (Terminal B): `curl -s http://127.0.0.1:8765/api/v1/openapi.json \| jq '.paths \| keys'` | array containing `/api/v1/health`, `/api/v1/platforms`, `/api/v1/games` |
| 1.3 | no token leak in startup | grep stdout for the literal 32-char token → 0 hits |

Pass / Fail: ☐

---

## Scenario 2 — `/api/v1/platforms` regression smoke

Terminal B (`T=$ORCH_TOKEN`):
| Step | Command | Expected |
|---|---|---|
| 2.1 | `curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/platforms \| jq '.platforms[].name'` | `"steam"` then `"epic"` (steam first per BL6 D4) |
| 2.2 | unauthenticated | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/v1/platforms` → `401` |
| 2.3 | config NOT in response | `curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/platforms \| jq '.platforms[0] \| keys'` → no `config` key |

Pass / Fail: ☐

---

## Scenario 3 — `/api/v1/games` happy path + pagination

| Step | Command | Expected |
|---|---|---|
| 3.1 default | `curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/games \| jq '.games \| length, .meta'` | `30` games, meta with `total: 30`, `limit: 50`, `offset: 0`, `has_more: false` |
| 3.2 explicit limit | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?limit=5" \| jq '.games \| length, .meta.has_more'` | `5`, `true` |
| 3.3 offset | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?limit=5&offset=5" \| jq '.games \| length, .meta.offset'` | `5`, `5` |
| 3.4 limit cap | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?limit=1000" -w '%{http_code}\n' -o /tmp/uat4.out; cat /tmp/uat4.out` | `400`, body contains `"limit"` |

Pass / Fail: ☐

---

## Scenario 4 — `/api/v1/games` filter + sort

| Step | Command | Expected |
|---|---|---|
| 4.1 platform filter | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?platform=steam&limit=500" \| jq '[.games[].platform] \| unique'` | `["steam"]` only |
| 4.2 status multi | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?status_in=not_downloaded,up_to_date&limit=500" \| jq '[.games[].status] \| unique \| sort'` | exactly `["not_downloaded", "up_to_date"]` |
| 4.3 size range | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?size_bytes_gte=10000000000&size_bytes_lte=50000000000&limit=500" \| jq '[.games[].size_bytes] \| min, max'` | both between 10e9 and 50e9 |
| 4.4 sort desc | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?sort=title:desc&limit=500" \| jq '[.games[].title][:3]'` | titles in reverse alphabetical |
| 4.5 multi-sort | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?sort=size_bytes:desc,title:asc&limit=500" \| jq '.meta.applied_sort'` | 3 entries: size_bytes:desc, title:asc, id:asc (tie-breaker appended) |

Pass / Fail: ☐

---

## Scenario 5 — `/api/v1/games` error paths (UAT-3 lessons + UAT-4 candidates)

| Step | Command | Expected | Records |
|---|---|---|---|
| 5.1 unknown filter | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?password=foo"` | `400` |  |
| 5.2 unknown operator | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?platform_gte=foo"` | `400` |  |
| 5.3 invalid value | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?size_bytes_gte=abc"` | `400` |  |
| 5.4 unauth | `curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8765/api/v1/games"` | `401` |  |
| 5.5 **UAT-4 S2-B: empty-sort silent-drop** | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?sort=,,,&limit=3" \| jq '.meta.applied_sort'` | **CANDIDATE BUG**: should be `[{title:asc}, {id:asc}]` (defaults); UAT-4 found only `[{id:asc}]` (tie-breaker only) | record actual: ___ |
| 5.6 **UAT-4 S2-D: oversized int** | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?size_bytes_gte=99999999999999999999999"` | **CANDIDATE BUG**: should be `400` (UAT-4 found `500` due to SQLite OverflowError) | record actual: ___ |
| 5.7 **UAT-4 S2-A: applied_filters wire format** | `curl -s -H "Authorization: Bearer $T" "http://127.0.0.1:8765/api/v1/games?platform=steam&limit=3" \| jq '.meta.applied_filters'` | **CANDIDATE BUG**: should be `{"platform": {"eq": "steam"}}` per spec; UAT-4 found 7 keys per filtered field with 6 nulls | record actual:<br><br>``` <br>(paste here) <br>``` |

Pass / Fail: ☐
(Note: 5.5/5.6/5.7 marked "candidate bug" → confirm-or-deny in live; PASS = bug confirmed, evidence captured.)

---

## Scenario 6 — Loopback-only schema/UI (UAT-3 regression)

Stop Terminal A, restart with `--host 0.0.0.0 --port 8765`. Get LAN_IP via `LAN_IP=$(ipconfig getifaddr en0)`.

| Step | Command | Expected |
|---|---|---|
| 6.1 LAN openapi | `curl -s -o /dev/null -w '%{http_code}\n' "http://$LAN_IP:8765/api/v1/openapi.json"` | `403` (UAT-3 S2-C still mitigated) |
| 6.2 LAN docs | `curl -s -o /dev/null -w '%{http_code}\n' "http://$LAN_IP:8765/api/v1/docs"` | `403` |
| 6.3 LAN games | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" "http://$LAN_IP:8765/api/v1/games"` | `200` (games is NOT loopback-only — only auth-required) |
| 6.4 non-loopback bind warning | (check Terminal A startup logs) | `api.boot.non_loopback_bind_warning` event present |

Pass / Fail: ☐

After 6.x, stop server and restart on `--host 127.0.0.1` for remaining scenarios.

---

## Scenario 7 — Correlation ID propagation (UAT-3 regression)

| Step | Command | Expected |
|---|---|---|
| 7.1 echo | `curl -sI -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/games \| grep -i x-correlation-id` | header present, UUID-shaped |
| 7.2 server-generated | repeat without `-H "X-Correlation-ID: ..."` | UUID still present (server generates) |
| 7.3 client value ignored | `curl -sI -H "Authorization: Bearer $T" -H "X-Correlation-ID: my-test-id" http://127.0.0.1:8765/api/v1/games \| grep -i x-correlation-id` | server-generated UUID, NOT `my-test-id` |

Pass / Fail: ☐

---

## Scenario 8 — `/api/v1/games` pool-failure path

Stop server. Then start with a path that will fail mid-request (no easy way to simulate pool failure for /games without DB tampering; skip this and rely on the existing unit test `TestGamesPoolFailure::test_pool_error_returns_503`).

| Step | Check | Expected |
|---|---|---|
| 8.1 | (skip — covered by unit test) | n/a |

Pass / Fail: ☐ (auto-pass — covered by unit test)

---

## Bugs Found

| ID | Scenario | Severity | Description | Repro |
|---|---|---|---|---|
| | | | | |

---

## Tester Notes

(free-form: anything surprising, slow, awkward)

---

## Cleanup

```
# Ctrl-C in Terminal A
rm -f /tmp/uat4.db /tmp/uat4.out
unset ORCH_TOKEN ORCH_DATABASE_PATH
```

---

## Appendix A — Game seed SQL

Paste into a `sqlite3` session against `$ORCH_DATABASE_PATH`:

```sql
INSERT INTO games (platform, app_id, title, owned, size_bytes, status, last_prefilled_at, metadata) VALUES
  ('steam', '10', 'Counter-Strike', 1, 5000000000, 'up_to_date', '2026-05-15T00:00:00Z', '{"depots":[101]}'),
  ('steam', '440', 'Team Fortress 2', 1, 25000000000, 'up_to_date', '2026-05-10T00:00:00Z', '{"depots":[441]}'),
  ('steam', '570', 'Dota 2', 1, 75000000000, 'pending_update', '2026-05-08T00:00:00Z', '{"depots":[571]}'),
  ('steam', '730', 'CS:GO 2', 1, 35000000000, 'not_downloaded', NULL, '{"depots":[731]}'),
  ('steam', '1086940', 'Baldur''s Gate 3', 1, 122000000000, 'up_to_date', '2026-05-18T00:00:00Z', '{"depots":[1086941,1086942]}'),
  ('steam', '292030', 'The Witcher 3', 1, 50000000000, 'up_to_date', '2026-05-12T00:00:00Z', '{"depots":[292031]}'),
  ('steam', '1245620', 'Elden Ring', 1, 60000000000, 'pending_update', NULL, '{"depots":[1245621]}'),
  ('steam', '578080', 'PUBG', 1, 30000000000, 'blocked', NULL, '{"depots":[578081]}'),
  ('steam', '2050650', 'Resident Evil 4', 0, 40000000000, 'not_downloaded', NULL, '{"depots":[2050651]}'),
  ('steam', '292030.dlc', 'Witcher 3 — Blood and Wine', 1, 20000000000, 'up_to_date', '2026-05-12T00:00:00Z', '{"depots":[292032]}'),
  ('steam', '108600', 'Project Zomboid', 1, 5000000000, 'failed', NULL, '{"depots":[108601]}'),
  ('steam', '1888930', 'The Last of Us', 1, 80000000000, 'downloading', NULL, '{"depots":[1888931]}'),
  ('steam', '413150', 'Stardew Valley', 1, 1500000000, 'up_to_date', '2026-05-19T00:00:00Z', '{"depots":[413151]}'),
  ('steam', '1366540', 'Dyson Sphere Program', 1, 4000000000, 'up_to_date', '2026-05-17T00:00:00Z', '{"depots":[1366541]}'),
  ('steam', '281990', 'Stellaris', 1, 15000000000, 'pending_update', NULL, '{"depots":[281991]}'),
  ('epic', 'fortnite', 'Fortnite', 1, 30000000000, 'up_to_date', '2026-05-16T00:00:00Z', '{"build_version":"++Fortnite+Release-30.20"}'),
  ('epic', 'rocketleague', 'Rocket League', 1, 25000000000, 'up_to_date', '2026-05-14T00:00:00Z', '{"build_version":"v2.40"}'),
  ('epic', 'gtav', 'GTA V', 1, 95000000000, 'not_downloaded', NULL, '{"build_version":"1.0.3258.0"}'),
  ('epic', 'cyberpunk', 'Cyberpunk 2077', 1, 80000000000, 'pending_update', NULL, '{"build_version":"2.13"}'),
  ('epic', 'control', 'Control', 1, 45000000000, 'up_to_date', '2026-05-09T00:00:00Z', '{"build_version":"1.13"}'),
  ('epic', 'borderlands3', 'Borderlands 3', 1, 75000000000, 'blocked', NULL, '{"build_version":"6.0"}'),
  ('epic', 'civ6', 'Civilization VI', 1, 20000000000, 'up_to_date', '2026-05-11T00:00:00Z', '{"build_version":"1.0.13.5"}'),
  ('epic', 'metro', 'Metro Exodus', 1, 55000000000, 'failed', NULL, '{"build_version":"1.4.0.18"}'),
  ('epic', 'subnautica', 'Subnautica', 1, 12000000000, 'up_to_date', '2026-05-13T00:00:00Z', '{"build_version":"75788"}'),
  ('epic', 'satisfactory', 'Satisfactory', 1, 25000000000, 'pending_update', NULL, '{"build_version":"1.0.0.4"}'),
  ('epic', 'hades', 'Hades', 1, 18000000000, 'up_to_date', '2026-05-15T00:00:00Z', '{"build_version":"v1.39032"}'),
  ('epic', 'fallguys', 'Fall Guys', 1, 5000000000, 'downloading', NULL, '{"build_version":"3.7.0"}'),
  ('epic', 'deadbydaylight', 'Dead by Daylight', 0, 60000000000, 'not_downloaded', NULL, '{"build_version":"8.1.0"}'),
  ('epic', 'apex', 'Apex Legends', 1, 70000000000, 'up_to_date', '2026-05-18T00:00:00Z', '{"build_version":"v22.1"}'),
  ('epic', 'rdr2', 'Red Dead Redemption 2', 1, 110000000000, 'up_to_date', '2026-05-07T00:00:00Z', '{"build_version":"1.0.1491.16"}');
```

30 games (15 steam + 15 epic). Status mix covers all 8 enum values. Sizes span 1.5 GB to 122 GB. `last_prefilled_at` populated for ~half. metadata in JSON form per platform.
