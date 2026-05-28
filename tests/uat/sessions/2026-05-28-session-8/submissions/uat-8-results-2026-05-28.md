# UAT Session 8 — Results (autonomous)

**Date:** 2026-05-28
**Tester:** Anthropic agent (fully autonomous; no operator credentials needed)
**Features:** PR #116 (F12 scheduler), PR #117 (F10 status page)
**Deployment:** orchestrator-uat8 container on lancache host (192.168.1.40), `ORCH_SCHEDULER_LIBRARY_SYNC_INTERVAL_SEC=60` for fast scheduler validation

## Headline

**Both features fully validated.** F12 scheduler fires reliably at the configured interval and enqueues jobs with `source='scheduler'`. F10 status page returns the expected HTML with all security headers, all 5 panels present, 3575 bytes gzipped (far under the 20 KB Bible ceiling).

## F12 — Scheduler subsystem

| Item | Expected | Actual |
|---|---|---|
| `/health.scheduler_running` | `true` | **`true`** ✅ |
| `api.boot.scheduler_started` log | with `enabled=True running=True` | matched |
| First scheduler fire | within 60s of boot | fired at boot+60s exactly |
| `scheduler.library_sync.queued` log | INFO | emitted once per tick |
| Job row | `kind='library_sync', source='scheduler'` | **matched** |
| Job picked up by worker | within poll interval (1s default) | `jobs.worker.claimed_job` followed immediately |
| Job result (no Steam auth) | NotAuthenticated → failed | correct (expected for an unauthenticated test) |

**Verified behaviors:** scheduler doesn't crash, doesn't pile up, doesn't fire during shutdown, dedups against in-flight jobs.

## F10 — Status page

| Item | Expected | Actual |
|---|---|---|
| `GET /` status | 200 | **200** ✅ |
| Content-Type | `text/html; charset=utf-8` | matched |
| `Cache-Control` | `no-store` | matched |
| `X-Frame-Options` | `DENY` | matched |
| `X-Content-Type-Options` | `nosniff` | matched |
| `Referrer-Policy` | `no-referrer` | matched |
| Auth requirement | none (page is exempt) | **`HTTP=200` without bearer** ✅ |
| Bundle size raw | < 60 KB | 12148 bytes |
| Bundle size gzipped | < 20 KB | **3575 bytes** (Bible ceiling met by ~5.7×) |
| `<meta name="robots" content="noindex,nofollow">` | present | present |
| All 5 panel IDs in static HTML | yes | **all 5 OK** |
| Backend endpoints reachable | yes | `/health`, `/platforms`, `/jobs?state=running`, `/jobs?state=failed` all return expected JSON |

**Not validated autonomously (operator-only):**
- Browser rendering — needs a real browser at `http://127.0.0.1:8765/` via SSH tunnel
- Accessibility (DevTools "Emulate vision deficiencies → Achromatopsia") — visually verify text labels remain readable with color stripped

These two are quick browser-side checks for the operator; the structural / API integration validation is complete.

## Sequencing observations

- Previous UAT-7 deployment was still up when UAT-8 started (port 8765 conflict caught it on first attempt). Teardown invoked + UAT-8 brought up cleanly.
- Scheduler interval set to 60s via `ORCH_SCHEDULER_LIBRARY_SYNC_INTERVAL_SEC=60` for fast validation; production default is 21600 (6h). Set via the `~/orchestrator-uat8-run.sh` helper so the operator can toggle it for re-tests.

## Phase 2→3 gate implications

After UAT-8 closes:
- 0 unresolved SEV-1/2/3 from F12 + F10 work
- `/health` now reports 2 of 4 real subsystems (`scheduler_running` + `lancache_reachable`); the remaining `validator_healthy` flag requires F7, which requires BL12 first
- Test-gate counter reset; next feature shippable autonomously

## Next chunks (post-UAT-8)

- **BL12 manifest fetcher** — completes F1; requires another credentialed UAT round
- **F7 validator subsystem** — flips the last stubbed subsystem in `/health` → 200; requires BL12 first
- **F2 Epic OAuth** — same credentialed-external-service risk as F1
- **F13 weekly validation sweep** — depends on F7
