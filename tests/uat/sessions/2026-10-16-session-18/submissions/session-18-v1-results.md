# UAT Session 18: results

**Tester:** Claude, executing at Karl's instruction (2026-10-16).
**Executed:** 2026-10-16, 13:30-15:30 UTC, against the live LXC 1105 and the NAS. Repo `main` @ `c7a3ee2`.
**Template:** `../templates/test-session-18-v1.md`.

| # | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | The weekly fetch logs in once | **Pass** | Job 46910 (2026-10-13 05:00-06:03Z): `manifest_fetch.done … logons=1, helper_exit=0, helper_outcome=completed, not_attempted=0` |
| 2 | SteamPrefill is not rate-limited on fetch day | **Pass** | `2026-10-13T06:31:57-06:00 END steam prefill ok`. The 06:00 MDT runs on 09-22, 09-29 and 10-06 ended `rc=1`. No `RateLimitExceeded` run block since 10-06. |
| 3 | The SteamPrefill monitor stays green | **Pass** | Kuma 176 `job:steam-prefill` UP. Last heartbeat `2026-10-16 12:28:46 OK`, and every heartbeat since 10-15 was UP. |
| 4 | Parity with DepotDownloader | **Pass** | 2026-10-08: 50 apps with DD-written `.shas` gave `identical=150 different=0 no_archived_copy=0` |
| 5 | The saved session is private | **Pass** | `-rw------- root root 538 Oct 8 14:21 session.json`. The directory was `drwx------` on 10-08. |
| 6 | The token never reaches logs | **Pass** | `docker logs orchestrator-agent \| grep -c "eyJ"` returns `0`, counted only and never printed. The helper's stderr tail holds only `logged on as kraulerson…`. |
| 7 | Per-app failures are visible with a reason | **Pass** | All 164 `manifest_fetch.app_failed` events carry a `status` and a `reason` |
| 8 | The weekly failure rate is within the alarm | **Fail** | `failed=164/1212` = 13.5%, above 7%. 148 failures are `depot key …: TaskCanceledException`. This is bug B1 (#369). |
| 9 | Helper error paths on the live binary | **Pass** | No args: `exit 64`, stdout 0 bytes. Missing option: `exit 64`, stdout 0 bytes. Corrupt session: `exit 3`, stdout is one JSON summary line `{"session":"import_failed",…,"logons":0,"summary":true}` that names the login command. Scratch files removed. |
| 10 | Rollback is available | **Pass** | `dpa-pre-361` is present on the LXC and on the NAS. `dpa-pre-370` is present on the LXC. |
| 11 | Automated suites | **Pass** | See `../agent-results/01-automated-suite.md`: pytest `2064 passed`, dotnet `60 passed`, ruff, mypy, semgrep (Python + C#) and gitleaks all clean |
| 12 | The alarm fires above 7% | **Pass** | 10-13 06:03:48Z `fetch_manifests.failure_ratio_exceeded`; Kuma 182 `job:orch-fetch-manifests` DOWN `apps=1212 … failed=164`. The alarm worked as designed. |
| 13 | The alarm stays green at or below 7% | **Pass** | Boundary tests in `tests/jobs/test_fetch_manifests_summary.py`: 59/1211 and 84/1211 UP, the exact threshold UP, 85/1211 DOWN. Part of the 2064 passing. |
| 14 | The live value is 7% | **Pass** | Running control plane: `live ratio 0.07` on 2026-10-08 21:25Z, image `d107862e`, unchanged since |
| 15 | Docs name the right monitor | **Fail** | `CHANGELOG.md:29`, the 7% security audit `:14` and the PR #370 text say "Kuma 176". kuma.db shows 176 = `job:steam-prefill` and 182 = `job:orch-fetch-manifests`. Bug B2. |
| 16 | The schedule matches the docs | **Fail** | `fetch_manifests_cron = "0 5 * * 1"` fires on **Tuesday**. APScheduler 3.11.3 `from_crontab` treats 0 as Monday, giving `next fire 2026-10-20T05:00:00+00:00 Tuesday`. The docs say Monday. Bug B3. |

## Bugs Found

| # | Severity | Feature | Description | Steps to Reproduce | Expected vs Actual |
|---|---|---|---|---|---|
| B1 | SEV-2 | #361 helper | Depot-key requests time out (`TaskCanceledException`) on 26 apps on 10-08 and 148 on 10-13. The 13.5% failure rate trips Kuma 182. Tracked as #369. | Read the scheduled `manifest_fetch.done` and its `app_failed` reasons | Expected: below 7% failed. Actual: 13.5%, with 148 depot-key timeouts. |
| B2 | SEV-3 | 7% alarm docs | The fetch alarm is documented as Kuma 176 instead of 182 | Compare `CHANGELOG.md:29` with kuma.db | Expected: 182. Actual: 176. |
| B3 | SEV-3 | scheduler | Any numeric weekday in a cron setting fires a day late (APScheduler 3.x counts 0 as Monday), so the "Monday" fetch runs on Tuesday | `CronTrigger.from_crontab('0 5 * * 1').get_next_fire_time(...)` | Expected: Monday. Actual: Tuesday. |
| B4 | SEV-3 | LXC host (outside repo) | The `orchestrator` container is stopped and started from outside (10-08 14:08Z, 10-10 08:00Z, after the 10-07 host halt). Each event reaps a running sweep. | `docker inspect orchestrator` shows StartedAt ≠ Created and RestartCount 0, while the LXC uptime is unchanged | Expected: no external restarts. Actual: 3 in 4 days. |

Exploratory findings: see `../agent-results/03-exploratory.md`.

## Overall Notes

#361's headline promise holds in production: one Steam login per run, SteamPrefill no longer locked out, Kuma 176 green. The weekly fetch's remaining failures are one cause, depot-key timeouts. They are mostly transient: on a 2026-10-16 re-run, 121 of the 148 apps that failed on 10-13 succeeded. Fixing them is the session's Fix Now item.
