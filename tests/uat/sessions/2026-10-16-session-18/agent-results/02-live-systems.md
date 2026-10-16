# UAT Session 18: live systems

**Run by:** Claude, 2026-10-16 13:30-14:45 UTC. Every check was read-only unless marked otherwise.

## Hosts and containers

| Item | Evidence | Verdict |
|---|---|---|
| LXC 1105 control plane | `orchestrator Up 6 days (healthy)`, image `d107862e` (7% build). `/api/v1/health` reports `status:ok`, `scheduler_running:true`, `agent_reachable:true`, `steam_auth_ok:true`. | Pass |
| NAS agent | `orchestrator-agent Up 7 days (healthy)`, image `ff7e6037` (#361 build), created 2026-10-08 14:19:17Z, `RestartCount 0`. | Pass |
| lancache, cache-catcher | `lancache-monolithic Up 13 days`, `cache-catcher Up 9 days`. | Pass |
| External restarts of `orchestrator` | Container started 2026-10-10T08:00:19Z with `RestartCount 0`; the LXC had not rebooted (`uptime -s` = 2026-10-07 07:37 MDT). This reaped sweep 46873. Earlier events: 10-07 13:35Z (host halt) and 10-08 14:08:49Z (container stop/start). The cause is outside the orchestrator. | Finding F3 (homelab) |
| LXC disk | 80% on 2026-10-16, against 54% on 10-07. Cause: 5.9 GB of Docker build cache from the #361 .NET builds. **Remediated during this session** (Karl option C2): 5 old `dpa-pre-*` tags removed and `docker builder prune` run, bringing it to **49%**, with 9.6 G free. | Fixed |

## The #361 promise

| Check | Evidence | Verdict |
|---|---|---|
| One Steam login per weekly run | job 46910 (2026-10-13 05:00-06:03Z): `manifest_fetch.done … logons=1 … helper_exit=0 helper_outcome=completed`. The first hand-run (job 46856, 10-08): `logons=1`. | Pass |
| SteamPrefill on fetch day | `2026-10-13T06:31:57-06:00 END steam prefill ok`. The fetch days before it failed: 09-22, 09-29 and 10-06 each ended `END steam prefill rc=1`, with `RateLimitExceeded` in those run blocks. No run block has contained `RateLimitExceeded` since 10-06. | Pass |
| Kuma 176 `job:steam-prefill` | UP, last heartbeat `2026-10-16 12:28:46 OK`. Every heartbeat since 10-15 was UP. | Pass |
| Parity on live data | 2026-10-08: the production helper fetched 50 apps, and all 150 manifests matched the `.shas` written by DepotDownloader (`identical=150 different=0`). | Pass |
| Weekly fetch failure rate | job 46910: `failed=164/1212` = **13.5%**. That is above the 7% default, so `fetch_manifests.failure_ratio_exceeded` fired, and **Kuma 182 `job:orch-fetch-manifests` has been DOWN since 2026-10-13 06:03**. 148 of the 164 failures are `depot key …: TaskCanceledException`; the 10-08 run had 26. | **Fail → F1 (#369)** |
| Kuma bindings | 176, 179-182, 216 and 217 are bound to notification 3. 218, 219 and 220 are UP. 181 (`job:orch-scheduled-prefill`) has been inactive since 2026-09-29; that predates this batch. | Info |

## Sweeps and jobs since the deploy (2026-10-08 18:00 → 2026-10-16)

- **Sweeps:** 28 succeeded and 1 failed. The failure was 46873, `orchestrator restarted while job was running (ID6 reaper)`, caused by the F3 external restart. Kuma 180 reads `pass 29 partial: 688/3083 games`.
- **library_sync:** 57 succeeded and 1 failed. The failure was 46881 on 10-10 at 20:01, with `ConnectTimeout`. It did not recur.

## Documentation accuracy check

- **F2:** `CHANGELOG.md:29`, `docs/security-audits/fetch-manifests-ratio-7pct-security-audit.md:14` and the PR #370 description say the fetch-ratio alarm is "Kuma 176". Kuma's own database shows 176 is `job:steam-prefill` and **182** is `job:orch-fetch-manifests`.
- **F4:** the older docs call the weekly fetch the "Monday 05:00 UTC" job. It actually runs on **Tuesday** at 05:00 UTC (Monday 23:00 MDT); the runs on 09-22, 09-29, 10-06 and 10-13 were all Tuesdays.
  - **Root cause:** `fetch_manifests_cron = "0 5 * * 1"` (`settings.py:311`) is parsed by APScheduler 3.x `CronTrigger.from_crontab` (`scheduler/manager.py:203`), which counts weekdays from 0 = **Monday**, so `1` = Tuesday. Standard cron counts 0 = Sunday.
  - **Proof:** `apscheduler 3.11.3 next fire 2026-10-20T05:00:00+00:00 Tuesday`.
  - **Scope:** only this cron has a numeric weekday; `validation_sweep_cron` and `scheduled_prefill_cron` use `*`. There is no live override in `/root/orch-lxc.env`.
  - **Risk:** any operator who writes a numeric weekday gets the day after the one they meant.

## Findings

| # | Finding | Severity |
|---|---|---|
| F1 | Weekly fetch fails 13.5% of apps, mostly on depot-key timeouts. Kuma 182 is DOWN. This is #369, which grew from 26 to 148. | SEV-2 |
| F2 | Docs and PR text tie the fetch alarm to Kuma 176; it is 182. | SEV-3 |
| F3 | The `orchestrator` container on LXC 1105 is stopped and started from outside the orchestrator (3 events in 4 days), and each event reaps a running sweep. | SEV-3 (outside the repo) |
| F4 | The cron weekday is off by one. The documented "Monday" fetch fires on Tuesday, because APScheduler 3.x's `from_crontab` counts weekdays as 0=Mon, so `0 5 * * 1` means Tuesday. | SEV-3 |
