# UAT Session 18: triage

**Session:** performed by Claude at Karl's instruction on 2026-10-16 ("perform the UAT yourself, only leave what I must absolutely do because you cannot"). Karl's decisions in this session:
- A1: run the UAT now and make #369 Fix Now.
- Keep the weekly fetch on Tuesday.
- B4: the homelab auto-update waits for an idle orchestrator.

Claude triaged everything else under that delegation, and Karl can overturn any line.

**Features under test:** Feature 30, the one-login Steam manifest helper (#361 / PR #368), and `fetch-manifests-ratio-7pct` (PR #370).

## Results summary

- **Scenarios:** 16 executed: 13 Pass, 3 Fail (8, 15 and 16). See `submissions/session-18-v1-results.md`.
- **Agents:** `01-automated-suite.md` all green; `02-live-systems.md` raised B1-B4; `03-exploratory.md` found 0 SEV-1, 0 SEV-2, 5 SEV-3 and 6 SEV-4.

## Triage

| # | Sev | Finding | Decision | Where |
|---|---|---|---|---|
| B1 | SEV-2 | Weekly fetch fails 13.5% of apps (148 depot-key timeouts); Kuma 182 DOWN | **Fix Now** (Karl A1) | #369. Fixed on `fix/369-depot-key-retry`, see below |
| B2 | SEV-3 | Docs and PR text name the fetch alarm as Kuma 176; it is 182 | **Fix Now** | Same branch |
| B3 | SEV-3 | A numeric cron weekday fires a day late (APScheduler 3.x counts 0 as Monday); the "Monday" fetch ran on Tuesday | **Fix Now**, keep Tuesday (Karl) | Same branch |
| B4 | SEV-3 | `orchestrator` on CT 1105 is restarted from outside (10-07, 10-08, 10-10), reaping sweeps | **Fixed outside the repo**, Karl option (b) | Homelab found the cause: its host reboot and weekly `auto-update.sh` (Sat 02:00 MDT = 08:00 UTC, inside the 03:00 sweep) restart docker. On Karl's choice, `auto-update.sh` will postpone docker-restarting steps while orchestrator jobs are running or queued. The check is a read-only jobs-table query, verified live. **Installed, as homelab confirmed 2026-10-16:**<br>• `/usr/local/bin/orch-idle-wait.sh` runs before `/root/auto-update.sh` in `/etc/cron.d/auto-update`.<br>• It proceeds only when `SELECT count(*) FROM jobs WHERE state IN ('running','queued')` returns 0 and the time is not within 10 min of a 03/09/15/21 UTC start.<br>• Otherwise it re-checks every 5 min for up to 8 h, then skips the week and sets Kuma 237 DOWN.<br>• An unreadable count fails loud.<br>**Proof** (`--check`): 18:41 UTC `busy:1` exit 1; 20:31 UTC `busy:3` exit 1; 20:36 UTC `idle` exit 0.<br>**Open, owned by homelab, due before 2026-11-01:**<br>• In MST, 02:00 local = 09:00 UTC, so the first second of a sweep reads idle; the fix treats :00–:04 of start hours as busy and raises the wait to 14 h.<br>• A manual `auto-update.sh` run bypasses the guard (the 10-08 kill of sweep 46853); manual runs are to use `orch-idle-wait.sh && /root/auto-update.sh`. |
| E1-E5 | SEV-3 | Exploratory findings 1-5 | **Defer** | #372 (reasons per item) |
| E6-E11 | SEV-4 | Exploratory findings 6-11 | **Defer** | #372 |
| — | — | Licence gate: skip depots the account does not own (DepotDownloader parity). Surfaced by the B1 diagnosis. | **Defer**, as an enhancement | #371 |

## B1 root cause, from the systematic-debugging investigation on 2026-10-16

- **Position analysis.**
  - The depot-key failures were not a fixed set of depots: only 3 of 26 repeated between 10-08 and 10-13.
  - 108 of the 148 failed depots had `.shas` written by DepotDownloader, so the account owns them.
- **Re-run on the 148 apps.** 121 now succeeded, so the failures are transient.
- **Instrumented build** (55 s timeout, per-request latency logged): 1,988 key requests.
  - 20 got no answer at all (1.01%).
  - Answered requests took 100 ms at p50 and 224 ms at most; none took between 10 and 55 s.
  - So requests are dropped, not slow.
  - 1,112 answers were `AccessDenied`, for unowned depots (#371).
- **Model.** About 12 key requests per app (up to 1,184 for app 24010) × 1% drop rate ≈ 11% of apps failing. Measured on 10-13: 148 of 1,212 = 12.2%.
- **Retry build** (3 attempts, logged): both dropped requests were answered on the 2nd attempt, in about 0.5 s. 145 of 148 apps were ok, and the 3 left were 2 CDN 503s and 1 missing request code. None were depot-key failures.

## Remediation (uncommitted while the session is open; committed after the checklist closes)

- **B1:** `RequestRetry` (3 attempts, retries only `SteamRequestException`) wraps the depot-key request in `SteamKitGateway.GetDepotKeyAsync`. `MapDepotKey` stays outside the retry, so explicit refusals are never re-sent.
  - Covered by 5 xUnit tests and a source guard (`RequestRetry.RunAsync(` with `attempts: 3`).
  - The logon count is unchanged: at most 2 per run.
- **B2:** 176 → 182 in `CHANGELOG.md` and the 7% security audit.
- **B3:**
  - The default is `fetch_manifests_cron = "0 5 * * tue"`, and the `SchedulerManager` default matches.
  - All three cron settings reject any digit in the day-of-week field.
  - The current-state docs say "Tuesday 05:00 UTC (Monday 23:00 MDT)", and README.md now shows the real sweep default.
- **Review:** one independent review approved B1 and B2. B3 needed two fixes (`*/2` and `mon,3` slipped through; a stale numeric README example), both now done.
- **Verified:** pytest `2104 passed, 3 deselected`; dotnet `Passed: 65`; ruff and mypy clean.
- **Live proof still due** after merge and deploy: one hand-started weekly fetch with depot-key failures near 0 and Kuma 182 UP.
