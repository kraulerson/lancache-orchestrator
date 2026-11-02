# UAT Session 17 — triage

**Status: CLOSED 2026-10-07.** Every finding from the three agents, Karl's
results, and one issue carried in from outside the session is below. Karl
accepted every recommendation as written on 2026-10-06, and confirmed #330 stays
SEV-3. On 2026-10-07 he re-triaged #361 from Fix Now to the next feature: its
fix grew from a code change into a new component, so it gets the full build loop
and its own test session. That left #362 and #363 as the only Fix Now items,
both shipped, so the session closes.

## Karl's results: 10 of 10 PASS

Submission: `submissions/session-17-v1-results.md`. No bugs were raised.

Two notes on the submission itself:

- **The Date field reads 2026-09-22.** Scenarios 7 and 8 can only pass after
  migration 0018 was deployed at 2026-09-23 20:46 UTC, so testing finished
  later than the field says. The Tester field reads `Unknown`.
- **Scenario 9 was confounded, and its PASS stands on database evidence.** It
  was written while CHUCHEL read `failed`, and expected that status to stay
  unchanged by the failed download of 2026-09-21. Migration 0018 then moved
  CHUCHEL to `blocked`, so the badge alone cannot show whether the download
  also wrote to it. The database shows it did not. CHUCHEL has exactly one
  status transition ever, and it is the migration's commanded one:

  ```
  status_measured_at       None
  last_job_outcome         prefill: EpicManifestError: epic manifest API failed: HTTP 404
  last_job_outcome_at      2026-09-21 20:32:20
  --- every status transition ever recorded ---
   ('2026-09-23 20:46:49', 'failed', 'blocked', 1, 1)
  ```

## Findings and recommendations

| Issue | Sev | Source | Title | Karl's decision |
|---|---|---|---|---|
| #355 | SEV-1 | exploratory E1 | NaN-poisoned history reads as healthy | **Done**: fixed, deployed 2026-09-23, closed |
| #361 | SEV-3 | carried in (homelab, 2026-10-06) | Weekly `fetch_manifests` logon storm rate-limits the Steam account | **Next feature** (re-triaged 2026-10-07; design approved) |
| #362 | SEV-3 | exploratory E2 | `kuma.push` raises on a non-string msg (latent) | **Fix Now**: fixed, PR #366, deployed 2026-10-07 |
| #363 | SEV-3 | exploratory E3 | Alarm drops a ceiling of 0, reports healthy | **Fix Now**: fixed, PR #366, deployed 2026-10-07 |
| #364 | SEV-4 | exploratory E5-E7 | Double-`?` URL, hidden sampling error, negative RAM budget | **Defer**, after checking item 1 (see below) |
| #312 | SEV-3 | exploratory E4 | Writer-guard evasions | **Defer**: three new forms added to the existing issue |
| #330 | SEV-3 | live-systems L1 | Epic prefill monitor 181 DOWN, no job enqueued | **Stays SEV-3**, queued (see below) |

Carried from earlier sessions and still open, already queued for later batches:
#331, #334, #315, #326, #348. **#346** stays deferred by Karl's decision: revisit
if monitor 218 goes DOWN.

## Reasoning (as presented for triage)

**#361** is the only finding still causing damage every week. Each Monday:
- 729 of 1,211 Steam apps get no fresh manifest.
- The 06:00 MDT SteamPrefill tick cannot log in.
- Kuma 176 goes DOWN.

The issue comment of 2026-10-06 adds the cause the report missed: since
2026-09-22 the job waits behind the Monday 03:00 UTC sweep. It also explains why
a reschedule alone cannot fix the 729 failures. The report's "regression of #228 /
#294" framing is **unverified**: per-run counts are not stored, and orchestrator
logs from before 2026-09-23 are gone.

**#362 is latent.** All six call sites pass a string today. The agent rated it
SEV-2 from the broken "Never raises" contract. It is filed as SEV-3 on
reachability, matching #334. Fixing it now is still cheap: the exception's real
consequence would be killing the guard from inside `alert()`.

**#363 needs a config typo** (`RAM_BUDGET_BYTES` under ~128 bytes) to trigger.
But when it does, it fails in the false-healthy direction this alarm was built
to rule out, which is why it is Fix Now despite the low reachability.

**#364 item 1 may be under-rated.** If an operator pastes a push URL that
already carries `?status=up&msg=OK&ping=`, `status` is sent twice. Which value
Kuma honours is unverified. If it is the first, a `down` push arrives as `up`:
a false-healthy, not a cosmetic bug. Check that before deferring.

**#330 severity.** The live-systems agent rated the monitor-181 finding SEV-2.
It had been DOWN continuously since at least 2026-09-16 when the agent looked,
and `jobs` showed zero scheduled Epic prefill jobs. #330 was filed as SEV-3.
If SEV-2, it must be resolved or removed before the Phase 2→3 gate. **Karl kept
it at SEV-3**: nothing is broken behind it. The scheduler runs and finds nothing
to download; the monitor cannot tell idle from dead.

**No unresolved SEV-1.** Per CLAUDE.md, SEV-1 cannot be deferred. The only one,
#355, is fixed and deployed.

## Remediation (2026-10-07)

- **#362 and #363** merged in PR #366 (`c0ef65b`). All 8 CI checks passed.
  Deployed to the NAS `cache-catcher` container at 03:18 UTC, after backing up
  the live files (`/log/kuma.py.bak-pre362`, `/log/key_budget.py.bak-pre363`).
  Kuma's own database recorded monitors 218, 219 and 220 UP from the restarted
  guard. The pre-merge adversarial review found that `RAM_BUDGET_BYTES=0` still
  read healthy; that was fixed before merge (`c84271a`).
- **New from that review:** #365 (a mistyped `FLOOR` reads healthy at 123%
  full; a non-UTF-8 env byte can kill the guard), and two edge cases recorded on
  #364. Both are untriaged for a later session.
- **#361:** root cause verified, and design approved by Karl on 2026-10-07:
  `docs/superpowers/specs/2026-10-06-steam-manifest-helper-design.md`. It is the
  next feature.

