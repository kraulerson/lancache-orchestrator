# UAT Session 7 — Results

**Date:** 2026-05-28
**Tester:** Anthropic agent (autonomous) + operator (kraulerson, 2FA round)
**Features:** PR #112 SEV-2 batch (F-UAT6-NEW-1 + NEW-3 + F-UAT6-1), PR #113 ID2, PR #114 ID6, PR #115 ID2 hotfix
**Deployment:** orchestrator-uat7 container on lancache host (192.168.1.40) via SSH

## Headline

**Every fix validated PASS against live Steam and live lancache.** The 5 SEV-2 fixes from the post-UAT-6 batch + ID2 + ID6 all work end-to-end as designed. ID2 had one deployment-surfaced bug (lancache returns 204 with header identifier, not 200) — fixed in PR #115 and validated live.

## Live-validated PASS

| Item | Evidence |
|---|---|
| **F-UAT6-NEW-1** (licenses dict iter + ClientLicenseList wait) | `library_sync.enumerate.returned app_count=2459`. Pre-fix: always 0. |
| **F-UAT6-NEW-3** (get_product_info batching + 5-min per-op IPC timeout) | Two complete enumerations in 99 s and 66 s — well under the 5-min budget. Pre-fix: 30 s IPC timeout fired. |
| **F-UAT6-1** (StreamReader 64 KiB limit) | 2459-app response flowed through the IPC channel without `ipc_response_overflow` log or `worker.died reason=response_too_large`. Pre-fix: reader crashed on `ValueError`. |
| **F-UAT6-2** (ORCH_STEAM_SESSION_DIR env wiring) | Worker `/proc/<pid>/environ` shows `ORCH_STEAM_SESSION_DIR=/var/lib/orchestrator/steam_session` |
| **F-UAT6-3** (auth_status='expired' on NotAuthenticated) | After NotAuthenticated handler exit, `/platforms` showed `auth_status: "expired"`, `last_error: "NotAuthenticated: no logged-in steam session"`. After successful re-auth, transitioned `expired → ok`. |
| **PR #115 fix** (lancache 204 + X-LanCache-Processed-By header) | `lancache.probe.state_changed reachable=true` against live lancache. Full cycle validated: up→`true`→stop lancache→`false`→start lancache→`true`. |
| **ID2** (lancache_reachable wiring) | `/health` shows `lancache_reachable: true`. |
| **ID6** (startup job reaper) | Inserted orphan `state='running'` row, `docker restart`, boot log: `jobs.reaper.reaped_orphans count=1`; row flipped to `failed` with reaper error message; `started_at` preserved. |
| **BL10 2FA auth** | Challenge issued (202 + `mobile_authenticator`), code accepted (200 + `steam_id`), platforms row updated, auto-trigger fired within 0 s |
| **BL11 library_sync** | Manual + auto-trigger both queue 202 + job_id; dedup returns same id; idempotent re-sync (2459 → 2459, no duplicates) |
| **Scenario 1** | Fresh boot — both platforms rows `auth_status: "never"` |
| **Scenario 5** | Bad password → 401 + `auth_status="error"` |
| **Scenario 7** | Unknown `challenge_id` → 404 |
| **Scenario 8** | `/auth/status` reflects live worker state (`authenticated: true, steam_id: ...`) |
| **Scenario 11** | Auto-triggered library_sync visible in `/jobs` immediately post-auth |
| **Scenario 13** | Manual sync → 202 + job_id |
| **Scenario 14** | Dedup — both POSTs return same job_id |
| **Scenario 15** | Re-sync preserved 2459 game count (idempotent UPSERT) |
| **Scenario 17** | Missing/wrong bearer → 401 both variants |
| **Scenario 18** | Structured events all fired in correct sequence; no `ipc_response_overflow` or `worker.died` during enumeration |

## Real-library spot check

Direct DB query confirmed these are in the games table with correct titles:

| app_id | title |
|---|---|
| 730 | Counter-Strike 2 |
| 440 | Team Fortress 2 |
| 570 | Dota 2 |
| 220 | Half-Life 2 |

Total games upserted: **2459**.

## Known limitations (per spike-2 / FRD)

- **#108 / docs/known-limitations.md**: session persistence not implemented for modern Steam accounts (steam-next 1.4.4 limitation). Container restart requires re-auth via 2FA.
- **/health returns 503** because `scheduler_running` and `validator_healthy` remain stub-false until those subsystems ship. ID2 alone doesn't flip /health to 200; the other two need their own BLs.
- **`cache_volume_mounted: false`** in /health — we didn't mount `/lancache/lancache/cache` into the orchestrator container for UAT-7 because the F7 validator subsystem isn't built yet.

## Bugs Found

### Bug 1 — [SEV-2 / fixed during session] ID2 probe required HTTP 200, lancache returns 204

**Filed and fixed as PR #115.** Discovered when deploying for UAT-7: real lancache returns HTTP 204 with `X-LanCache-Processed-By` header (not 200). PR #113's strict 200 check would have reported `lancache_reachable: false` even against healthy lancache. Fix accepts any 2xx + requires the header for positive identification. Validated live during this session.

## Overall Notes

**What worked well:**
- Agent-sweep approach in UAT-6 caught 3 SEV-2s before operator time was burned
- This UAT-7 caught 1 more (PR #115) during deployment, also before substantial operator involvement
- 2 Steam 2FA codes consumed total for UAT-7 (one for init, one for completion) — minimal operator friction
- ID6 was validated entirely without operator credentials (DB manipulation + docker restart)

**Validation maturity:**
- All 3 SEV-2 fixes from the UAT-6 batch now have live evidence on top of unit tests
- F1 (BL10 + BL11) is functionally validated — first end-to-end pass against real Steam library
- BL12 (manifest fetcher) is now unblocked from the "Spike-A-drift risk" that prevented implementation post-UAT-6

**Phase 2→3 gate implications:**
- All known SEV-2s either fixed (#107, #109 via PR #112) or documented as won't-fix (#108)
- Strategic follow-ups #111 (steam-next maintenance), #37 (F18 cache-purge), #77 (lancache health audit) remain open but don't block Phase 2→3
- F2 (Epic OAuth) + F5/6 (CDN prefill) + F7 (validator) + F10 (status page) + F12 (scheduler) + F13/F14-17 (Game_shelf) — substantial MVP scope remains

## Next sessions can proceed with

After PR #115 merges, the test-gate counter clears (UAT-7 resets `features_since_last_test`). Autonomous-shippable candidates:

- F7 validator subsystem (would flip `validator_healthy` to real)
- F12 scheduler subsystem (would flip `scheduler_running` to real)
- F10 status page
- F1 **BL12 manifest fetcher** (now fully unblocked — F-UAT6-NEW-1/3 + F-UAT6-1 validated, library populates correctly)
