# UAT Test Session 18

**Date:** 2026-10-16
**Features Under Test:**
- Feature 30: one-login Steam manifest helper (#361, PR #368)
- `fetch-manifests-ratio-7pct`: weekly manifest-fetch alarm at 7% (PR #370)

**Tester:** Claude. Karl, 2026-10-16: "perform the UAT yourself, only leave what I must absolutely do because you cannot".

The Markdown template is used rather than the HTML form, because no human tester fills this session in. Every scenario is executed against the live systems, or against the test suites where noted. Results are in `../submissions/session-18-v1-results.md`.

---

## Instructions

1. Follow each scenario step by step.
2. Mark it Pass or Fail, and put the evidence in the results file.
3. Record every Fail under Bugs Found, with a severity.

---

## Test Scenarios

### Feature: one-login Steam manifest helper (#361)

| # | Scenario | Steps | Expected Result |
|---|---|---|---|
| 1 | The weekly fetch logs in to Steam once | Read the latest scheduled `manifest_fetch.done` in `docker logs orchestrator-agent` | `logons: 1`, `helper_exit: 0`, `helper_outcome: completed`, `not_attempted: 0` |
| 2 | SteamPrefill is no longer rate-limited on fetch day | In `steamprefill-cache/cron.log`, read the 06:00 MDT run on the fetch day after the deploy, and count `RateLimitExceeded` per run block | `END steam prefill ok`, and no `RateLimitExceeded` block after the deploy |
| 3 | The SteamPrefill monitor stays green | Read Kuma 176 `job:steam-prefill` (kuma.db, read-only) | UP across the fetch day |
| 4 | Fetched manifests match DepotDownloader's | Run the production helper on 50 apps that have `.shas` written by DepotDownloader, then compare chunk-SHA lists | `different=0` |
| 5 | The saved Steam session is private | `ls -la /depotdownloader-config/steam-manifest-helper/` in the agent | Directory `drwx------`, `session.json` `-rw-------` |
| 6 | The token never reaches logs | `grep` agent logs and the helper's stderr tail for the token's prefix pattern (never print the token) | No match |
| 7 | Per-app failures are visible with a reason | Count `manifest_fetch.app_failed` and their `reason` fields for the run | Every failed app is logged with a reason |
| 8 | The weekly failure rate is within the alarm | `failed/apps` for the scheduled run | ≤ 7% |
| 9 | Helper error paths on the live binary | No args, a missing option, and a corrupt or missing session, all against scratch paths | Exit codes 64 / 64 / 3, with stdout carrying JSON only |
| 10 | Rollback is available | `docker images orchestrator` on both hosts | `dpa-pre-361` present on the LXC and the NAS |
| 11 | Automated suites | pytest, dotnet test, ruff, mypy, semgrep (Python + C#), gitleaks | All green |

### Feature: weekly manifest-fetch alarm at 7%

| # | Scenario | Steps | Expected Result |
|---|---|---|---|
| 12 | The alarm fires above 7% | Find a scheduled run above 7%, then read `fetch_manifests.failure_ratio_exceeded` and the fetch monitor in Kuma | Warning logged, fetch monitor DOWN |
| 13 | The alarm stays green at or below 7% | Boundary tests: 59/1211, 84/1211, the exact threshold, and 85/1211 | Up / Up / Up / Down |
| 14 | The live value is 7% | `get_settings().fetch_manifests_max_failure_ratio` in the running control plane | `0.07` |
| 15 | Docs name the right monitor | Compare CHANGELOG, the audit and the PR text against kuma.db's monitor names | The fetch alarm is identified as Kuma 182 |
| 16 | The schedule matches the docs | Compare `fetch_manifests_cron` with the actual weekday the job fires | The documented day is the firing day |

---

## Bugs Found

Recorded in `../submissions/session-18-v1-results.md` and `../triage.md`.

### Severity Guide
- **SEV-1:** data loss, a security breach, or the app crashing on a core flow
- **SEV-2:** a feature is broken but a workaround exists, or a significant UX failure
- **SEV-3:** a minor UX issue, cosmetic, or a non-core edge case
- **SEV-4:** an enhancement, suggestion or polish
