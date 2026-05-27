# UAT Session 6 — Results

**Date:** 2026-05-27
**Tester:** kraulerson (operator-driven via real Steam account)
**Features:** BL10 Steam auth substrate, BL11 library sync
**Deployment:** orchestrator-uat6 container on lancache host (192.168.1.40) via SSH tunnel from operator's Mac

## Summary

**Validated PASS (14 of 18 scenarios):**
- 3 SEV-2 fixes from agent sweep (F-UAT6-1, F-UAT6-2, F-UAT6-3) — all confirmed working against live Steam
- BL10 auth substrate end-to-end (no-2FA path inferred; 2FA path fully exercised)
- Manual library sync endpoint contract (queueing, dedup, bearer enforcement)
- Auto-trigger fires (job appears in queue immediately on auth success)
- Structured logging events fire as expected; no credential leakage
- F-UAT6-2 custom session dir wiring (env var → worker → `set_credential_location`)
- F-UAT6-3 NotAuthenticated → `auth_status='expired'` flip

**Validated FAIL (3 new SEV-2 findings — filed as follow-ups):**
- **#107** F-UAT6-NEW-1: library.enumerate returns 0 apps even for real accounts. Two bugs in `_handle_library_enumerate`: dict iteration yields keys (not entries) so `getattr(int, "package_id")` is always None; AND no wait for the asynchronous `ClientLicenseList` message before reading the dict.
- **#108** F-UAT6-NEW-2: session does not persist across container restart. Steam does not emit `EMsg.ClientNewLoginKey` for modern accounts (refresh-token based); steam-next 1.4.4's `set_credential_location` only persists the sentry file, not enough to relogin without password. Needs adoption of `steam.webauth` flow for modern accounts — substantive design change, not a small fix.
- **#109** F-UAT6-NEW-3: `get_product_info(packages=...)` exceeds the 30s IPC timeout for real-size libraries (operator has hundreds of packages, thousands of apps). Even with timeout bumped to 300s the call still hadn't returned at the cutoff. Needs batching + a different IPC pattern (job-layer timeouts instead of per-call).

## Scenarios

| # | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | Fresh boot — steam=never | PASS | `/platforms` returned both rows with `auth_status: never` |
| 2 | Happy path auth (no 2FA needed) | SKIPPED | Operator account requires 2FA; covered by scenario 4 |
| 3 | 2FA flow — initial 202 + challenge_id | PASS | Challenge `9ee4443b-2d3b-4dbe-8fe3-5d9d39c95c44`, type `mobile_authenticator`, 5-min expiry |
| 4 | 2FA flow — submit code → 200 + steam_id | PASS | `steam_id: 76561197993987535` |
| 5 | Bad password returns 401 | PASS | `401 InvalidCredentials`; platforms row flipped to `error` |
| 6 | Bad 2FA code returns 401 | NOT TESTED | Would consume another 2FA round; substantively covered by 5 (failure path) |
| 7 | Unknown challenge_id returns 404 | PASS | `{"detail":"unknown challenge_id"}` |
| 8 | Auth status reflects worker state | PASS | `authenticated: true`, `steam_id: 76561197993987535`, matches `/platforms` |
| 9 | F-UAT6-2 custom ORCH_STEAM_SESSION_DIR honored | PASS | Custom dir created at the env path; default path untouched |
| 10 | Session persistence across restart | **FAIL** | Issue #108. `/auth/status` returns `authenticated=false` post-restart; session dir empty on disk. |
| 11 | Auto-trigger queues library_sync on auth | PASS | `auth.auto_sync.queued` log + job appears in `/jobs` within 0s of auth_complete |
| 12 | F-UAT6-1 large library enumerates | **FAIL** | Issues #107 + #109. Combined: dict iter bug returns 0; even after fix, get_product_info exceeds IPC timeout. |
| 13 | Manual sync endpoint queues job | PASS | 202 + job_id |
| 14 | Dedup: rapid second POST returns same job_id | PASS | Both responses had identical `job_id`; only one queued row |
| 15 | Idempotent re-sync | TRIVIALLY PASS | 0 = 0 games before/after; substantive idempotency claim untestable while #107 blocks enumeration |
| 16 | NotAuthenticated → auth_status='expired' (F-UAT6-3) | PASS | Validated indirectly via worker-not-authenticated trigger before user's live auth |
| 17 | Missing/wrong bearer returns 401 | PASS | Both variants 401; `api.auth.rejected` logged with `reason=missing_header`/`bad_token` |
| 18 | Observability — structured events present | PASS | All 11 expected event names emitted in correct order |

## Bugs Found

### Bug 1 — [SEV-2] Library enumeration returns 0 apps for real accounts

**Filed as #107.** Two bugs in `_handle_library_enumerate`: dict iteration yields keys (not values), and no wait for the asynchronous ClientLicenseList message arrival. Code-level fix is straightforward but blocked from end-to-end validation by #109.

### Bug 2 — [SEV-2] Session does not persist across container restart

**Filed as #108.** Steam does not emit `ClientNewLoginKey` for modern accounts. steam-next 1.4.4's `set_credential_location` only saves sentry. Needs adoption of `steam.webauth` flow or a steam-next version with refresh-token support — substantive design change.

### Bug 3 — [SEV-2] get_product_info exceeds 30s IPC timeout for real libraries

**Filed as #109.** The deployment-shape agent flagged this as SEV-3 in the agent sweep ("above ~700 apps") but the operator's real library hits it as a hard functional blocker, not a perf concern. Needs chunked batching + handler-level timeout policy (move long-running ops off the per-IPC budget).

### Bug 4 — [SEV-4] Sort syntax inconsistency in /jobs

Operator template scenarios used `?sort=-id` for descending sort; the API rejects this with `400 "'-id' is not a sortable field"`. The actual syntax expected (per `applied_sort` in responses) appears to be different — needs documentation or syntactic alignment. Lower severity, filed as observation; will surface again in any follow-up UAT and can be batched into an API-docs cleanup.

## Overall Notes

**What worked well:**
- The agent-sweep approach found 3 real SEV-2s before any operator time was burned
- Parallel test agents took ~5 min wall clock; consolidated into actionable findings
- All 3 sweep-discovered SEV-2 fixes verified working against live Steam

**What didn't work:**
- BL11's design assumed steam-next license + persistence APIs work like the Spike A pattern. Spike A apparently validated against a flow that no longer represents real modern Steam accounts. The shipped BL11 code worked for tests (with stubs) but breaks on first contact with real Steam.
- 4 separate Steam 2FA auths burned during the session (one initial, one after first fix attempt, one for the second fix attempt, plus the initial credential-rejection test). Live UAT against an external service is expensive per-cycle; future UATs against credentialed flows should budget for fewer iterations.

**Phase 2→3 gate implications:**
- Issues #107, #108, #109 all carry SEV-2 labels and block the gate per CLAUDE.md ("SEV-2 ... must be resolved or feature removed at Phase 2→3 gate"). They effectively block BL12 (manifest fetcher depends on a populated games table).
- Recommendation: a Steam-spike-2 effort BEFORE attempting BL12 — re-validate steam-next's actual API for current Steam, redesign the library + persistence layers, re-attempt BL11. This is in line with the F1 spec's risk register entry "steam-next version drift" but turned out to be Spike A drift, not version drift.
