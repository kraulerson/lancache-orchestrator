# UAT Session 17 — triage

**Status: PROVISIONAL, and not yet consolidated.** Karl's scenario results have
not been received, so `bugs_consolidated` is not marked. This file exists early
to carry an issue found outside the session into it. The rest of the session's
findings join it at consolidation. Everything below is a recommendation awaiting
Karl's Fix Now / Defer / Won't Fix decision.

| Issue | Sev | Title | Recommendation |
|---|---|---|---|
| #361 | SEV-3 | Weekly `fetch_manifests` logon storm rate-limits the Steam account | **Fix Now**: recurring weekly production defect |

## #361 — carried in from outside the session

Filed 2026-10-06 by the homelab session, not found by a UAT 17 scenario. Karl
directed it into this session's triage the same day.

What it costs every Monday:
- 729 of 1,211 Steam apps get no fresh manifest, so update detection for ~60%
  of the library runs on stale data.
- The 06:00 MDT SteamPrefill tick cannot log in, so new purchases wait an
  extra 6 h, and Kuma 176 goes DOWN weekly, which trains the operator to ignore it.

The issue comment of 2026-10-06 verifies the report against the live DB and
`cron.log`, and adds one cause the report did not name. Since 2026-09-22 the job
waits in the queue behind the Monday 03:00 UTC sweep, which moved its storm
across the 06:00 MDT tick.

**Recommendation rationale.** A reschedule could remove the Kuma 176 symptom,
but it cannot fix the 729 failures: those happen whenever the job runs. The fix
belongs in the logon path (one session per batch, back off on refusal), as the
report proposes. Note this is a fix, so it is blocked until this session's
checklist completes.

**Unverified.** The report's "regression of #228 / #294" framing. Per-run counts
are not stored, and orchestrator logs from before 2026-09-23 are gone, so the
earlier failure rate cannot be compared.
