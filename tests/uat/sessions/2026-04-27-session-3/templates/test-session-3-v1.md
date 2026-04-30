# UAT Test Session — 3 (v1)

**Date:** 2026-04-27
**Features Under Test:** BL5 — FastAPI skeleton (`src/orchestrator/api/`)
**Tester:** Karl (Orchestrator)
**Format:** H-1 lightweight (HTTP API surface, manual `curl` flows)

---

## Instructions

1. Open two terminals from project root inside the venv: `source .venv/bin/activate`.
2. **Terminal A** runs the server. **Terminal B** runs the curl probes.
3. Mark Pass/Fail per row.
4. If Fail: fill in the Bugs Found table at the bottom.
5. When done, save this file to `tests/uat/sessions/2026-04-27-session-3/submissions/test-session-3-v1.md` and tell the Orchestrator agent "results are in".

> Health endpoint is **503 by design** in BL5 (Bible §8.4 / D6 / ADR-0012). 503 is PASS. 200 would be a regression.

---

## Pre-flight

| # | Check | Command | Expected |
|---|---|---|---|
| P1 | venv active (both terminals) | `which python` | `.../lancache_orchestrator/.venv/bin/python` |
| P2 | branch | `git branch --show-current` | `feat/uat-3-session` |
| P3 | clean tree | `git status --short` | (empty or only `M .claude/process-state.json`) |
| P4 | API test baseline | `pytest tests/api/ -q` | 48 pass |
| P5 | full suite green | `pytest -q` | 329+ pass |
| P6 | configure test token (Terminal A & B) | `export ORCH_TOKEN=$(printf 'a%.0s' {1..32}); export ORCH_DATABASE_PATH=/tmp/uat3.db; rm -f $ORCH_DATABASE_PATH` | (no output) |
| P7 | seed migration | `python -m orchestrator.db.migrate "$ORCH_DATABASE_PATH"` | exit 0; `applied_count=1` |

Pre-flight all-pass: ☐

---

## Scenario 1 — uvicorn boot, lifespan logs, schema check

Terminal A:
```
uvicorn orchestrator.api.main:app --host 127.0.0.1 --port 8765 --log-level info
```

| Step | Check | Expected |
|---|---|---|
| 1.1 | startup logs | sees `pool_initialized` and FastAPI Application startup complete on 127.0.0.1:8765 |
| 1.2 | no token leak | grep stdout for the literal token value `aaaaaaaa…` (32 a's) → **0 hits** |
| 1.3 | OpenAPI schema sanity (Terminal B) | `curl -s http://127.0.0.1:8765/openapi.json \| jq '.paths \| keys'` → contains `/api/v1/health` |

Pass / Fail: ☐

---

## Scenario 2 — /api/v1/health unauth: 503 by design

Terminal B:
| Step | Command | Expected |
|---|---|---|
| 2.1 | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/v1/health` | `503` |
| 2.2 | `curl -s http://127.0.0.1:8765/api/v1/health \| jq` | JSON object with 7 fields including `scheduler_running:false`, `lancache_reachable:false`, `validator_healthy:false`, `version`, `git_sha` |
| 2.3 | inspect | **NOTE the `git_sha` field** — UAT-3 flagged this as SEV-2 (live recon leak). Record what value is shown. (No action needed; triage decision after this session.) |
| 2.4 | response correlation header | `curl -sI http://127.0.0.1:8765/api/v1/health \| grep -i x-correlation-id` → present, UUID-shaped |

Pass / Fail: ☐
Recorded `git_sha`: ___________________

---

## Scenario 3 — Bearer auth happy / sad / malformed / wrong-scheme

Terminal B (replace `$T` with your configured token):
```
T="$ORCH_TOKEN"
```

| Step | Command | Expected |
|---|---|---|
| 3.1 happy | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" http://127.0.0.1:8765/api/v1/some-nonexistent-endpoint` | `404` (auth passed; routing failed) |
| 3.2 wrong token | `WRONG=zzz; curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $WRONG" http://127.0.0.1:8765/api/v1/some-nonexistent-endpoint` | `401` |
| 3.3 missing | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/v1/some-nonexistent-endpoint` | `401` |
| 3.4 wrong scheme | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Basic abc==" http://127.0.0.1:8765/api/v1/some-nonexistent-endpoint` | `401` |
| 3.5 lowercase scheme | `curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: bearer $T" http://127.0.0.1:8765/api/v1/some-nonexistent-endpoint` | `404` (HTTP scheme is case-insensitive per RFC) — record actual: ___ |
| 3.6 health is exempt (no auth needed) | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/v1/health` | `503` (exempt; reaches handler) |
| 3.7 timing | `for i in 1 2 3; do time curl -s -o /dev/null -H "Authorization: Bearer wrong" http://127.0.0.1:8765/api/v1/some-endpoint; done` | rough sanity check — no 10x outlier indicating timing leak |

Pass / Fail: ☐

---

## Scenario 4 — CORS preflight

| Step | Command | Expected |
|---|---|---|
| 4.1 | `curl -s -i -X OPTIONS -H "Origin: http://localhost:3000" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: Authorization,Content-Type" http://127.0.0.1:8765/api/v1/health` | `204` or `200`; **no `Access-Control-Allow-Origin` header** because `localhost:3000` is not in the default `cors_origins` list |
| 4.2 | repeat with no Origin header | preflight 405 or 200 (FastAPI default; no CORS headers) |

Pass / Fail: ☐

---

## Scenario 5 — Body size cap (32 KiB)

| Step | Command | Expected |
|---|---|---|
| 5.1 under cap | `head -c 16384 < /dev/urandom \| curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" -H "Content-Type: application/octet-stream" --data-binary @- http://127.0.0.1:8765/api/v1/some-endpoint` | `404` (auth ok, no route, body never consumed) |
| 5.2 at cap exactly (32768) | `head -c 32768 < /dev/urandom \| curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" -H "Content-Type: application/octet-stream" --data-binary @- http://127.0.0.1:8765/api/v1/some-endpoint` | `404` (still under-or-equal — record actual) |
| 5.3 cap + 1 (32769) | `head -c 32769 < /dev/urandom \| curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" -H "Content-Type: application/octet-stream" --data-binary @- http://127.0.0.1:8765/api/v1/some-endpoint` | `413` |
| 5.4 way over (1 MiB) | `head -c 1048576 < /dev/urandom \| curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" -H "Content-Type: application/octet-stream" --data-binary @- http://127.0.0.1:8765/api/v1/some-endpoint` | `413` |

Pass / Fail: ☐

---

## Scenario 6 — Loopback-only paths from non-loopback (negative)

This requires a second binding. Stop Terminal A's server, restart with:
```
uvicorn orchestrator.api.main:app --host 0.0.0.0 --port 8765
```
Then in Terminal B, get the LAN IP:
```
LAN_IP=$(ipconfig getifaddr en0 || hostname -I | awk '{print $1}')
echo "$LAN_IP"
```

| Step | Command | Expected |
|---|---|---|
| 6.1 loopback OpenAPI | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/openapi.json` | `200` (UAT-3 SEV-2 flagged this — record actual) |
| 6.2 LAN OpenAPI (CONCERN) | `curl -s -o /dev/null -w '%{http_code}\n' "http://$LAN_IP:8765/openapi.json"` | **expected to be `403` per OQ2 — but UAT-3 SEV-2 candidate F-6 says it may currently be `200`. Record actual.** |
| 6.3 LAN /docs | `curl -s -o /dev/null -w '%{http_code}\n' "http://$LAN_IP:8765/docs"` | record actual |
| 6.4 LAN /redoc | `curl -s -o /dev/null -w '%{http_code}\n' "http://$LAN_IP:8765/redoc"` | record actual |
| 6.5 LAN health | `curl -s -o /dev/null -w '%{http_code}\n' "http://$LAN_IP:8765/api/v1/health"` | `503` (exempt + non-loopback OK because health is intentionally accessible) |

Pass / Fail: ☐
Findings recorded for triage:
- 6.1 status: ___
- 6.2 status: ___
- 6.3 status: ___
- 6.4 status: ___

After this scenario, restart server back on `--host 127.0.0.1` for remaining scenarios.

---

## Scenario 7 — Lifespan failure path (migration error)

Stop server. Then:
```
export ORCH_DATABASE_PATH=/dev/null/cant-write-here
uvicorn orchestrator.api.main:app --host 127.0.0.1 --port 8765
```

| Step | Check | Expected |
|---|---|---|
| 7.1 startup fails fast | uvicorn process exits non-zero within ~5s | exit code != 0 |
| 7.2 log says why | stderr contains `migration` or `pool` failure indicator | yes |
| 7.3 no token in logs | grep stderr for the literal 32-char token value | 0 hits |

Reset: `export ORCH_DATABASE_PATH=/tmp/uat3.db`

Pass / Fail: ☐

---

## Scenario 8 — Correlation ID echo + injection probe

Restart server: `uvicorn orchestrator.api.main:app --host 127.0.0.1 --port 8765`

| Step | Command | Expected |
|---|---|---|
| 8.1 client supplies ID | `curl -sI -H "X-Correlation-ID: my-test-id-12345" http://127.0.0.1:8765/api/v1/health \| grep -i x-correlation-id` | record actual — UAT-3 SEV-4 flagged: server may accept-and-use, regenerate-and-replace, or reject |
| 8.2 garbage ID | `curl -sI -H "X-Correlation-ID: $'\nX-Injected: pwned'" http://127.0.0.1:8765/api/v1/health \| grep -iE 'x-correlation-id\|x-injected'` | should NOT show `X-Injected` in response (header smuggling safe) |
| 8.3 no ID supplied | `curl -sI http://127.0.0.1:8765/api/v1/health \| grep -i x-correlation-id` | UUID-shaped value present |

Pass / Fail: ☐

---

## Bugs Found

| ID | Scenario | Severity (SEV-1/2/3/4) | Description | Repro |
|---|---|---|---|---|
| | | | | |

---

## Tester Notes

(free-form: anything surprising, slow, awkward, or that needs follow-up)

---

## Cleanup

```
# Stop uvicorn (Ctrl-C in Terminal A)
rm -f /tmp/uat3.db
unset ORCH_TOKEN ORCH_DATABASE_PATH
```
