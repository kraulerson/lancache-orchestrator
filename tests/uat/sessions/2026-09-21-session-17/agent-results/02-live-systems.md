# UAT Session 17 — Live-Systems Verification

Agent run: 2026-09-21, ~22:10–22:14 UTC. Read-only against all three hosts. No writes, restarts, recreates, or job triggers were issued anywhere.

## Verdict: PASS-WITH-FINDINGS

## Findings table

| # | Area | Severity | Summary |
|---|------|----------|---------|
| 1 | Epic scheduled prefill (job:orch-epic-prefill, Kuma monitor 181) | SEV-2 | Monitor 181 has been DOWN ("No heartbeat in the time window") continuously since at least 2026-09-16, and the `jobs` table shows **zero** `source='scheduler'` Epic prefill jobs since 2026-09-01 (all that day's queued jobs were cancelled). `ORCH_SCHEDULED_PREFILL_ENABLED=true` is set. Not one of the three explicitly-scoped checks, but it is a live-health anomaly that contradicts CLAUDE.md's 2026-09-11 note that "the three most recent prefill cycles each enqueued 0 downloads" (implying the cycle *runs* and finds nothing) — what's actually observed is no evidence the scheduled cycle executes at all. Could be by design (no job row created when there are 0 candidates, and the push heartbeat only fires from inside that code path) — I could not confirm this from live systems alone; needs a code read. Flagging for follow-up, not fixing. |
| 2 | Monitor 218 key-budget only 3 heartbeats total | none — expected | CLAUDE.md (written 2026-09-19) says "the 24h gauge cycle... has still never fired on a real slot." Live data now shows 3 successful daily heartbeats (09-18, 09-19, 09-20, all UP) plus 3 CSV rows at the same ~24h cadence. This is not a contradiction, it's the passage of 3 more days since that doc note was written — the doc is stale, not wrong. Next heartbeat was due ~22:37 UTC today (2026-09-21), i.e. ~24 min after this check ran — not yet overdue at observation time. |
| 3 | Migration 0018 | none | Confirmed NOT deployed (highest applied = 0017), as expected/intended. |
| 4 | #317 verification | none | Holds exactly as specified — see evidence below. |
| 5 | Sweeps, breaker, agent, memory | none | All nominal — see evidence. |

## Evidence

### 1. Keys_zone alarm — Kuma monitors 218/219/220

Monitor IDs confirmed against group 119:
```
(119, 'host: lancache-orchestrator (1105)', 1, 'group')
(218, 'lancache:key-budget', 1, 'push')
(219, 'lancache:cache-guard', 1, 'push')
(220, 'lancache:cache-eviction', 1, 'push')
```

Heartbeat counts (count, down_count, min_time, max_time):
```
218 -> (3, 0, '2026-09-18 22:37:10.707', '2026-09-20 22:37:26.274')
219 -> (153, 0, '2026-09-18 22:37:02.896', '2026-09-21 22:07:11.786')
220 -> (155, 1, '2026-09-18 22:37:02.903', '2026-09-21 22:07:11.794')
```

- **218 (key-budget):** 3/3 heartbeats UP, 0 down. Daily cadence confirmed (~24h apart). Last: `35.7M objects, 48% of ram ceiling 75.0M, trend unknown` at 2026-09-20 22:37:26. Not overdue at time of check (next due ~22:37 today).
- **219 (cache-guard):** 153/153 UP, 0 down. 15-min cadence, most recent `2026-09-21 22:07:11.786` — `guard alive; evict 12/60s window, purges 0`.
- **220 (cache-eviction):** 155 total, 1 DOWN. The single DOWN beat is `2026-09-19 23:07:02.138 — lancache ALERT: cache eviction detected (10 deletes/60s) by other`, which matches the documented deliberate test trip at ~23:07 on 2026-09-19 (latches 3600s). No DOWN beats since. Confirmed expected, not a live fault.

### `/log/key_budget.csv` on cache-catcher (all 3 rows, verbatim)
```
1789771023,35782656,4598390784,128.50892857142858
1789857431,35670016,4598689792,128.9231210885916
1789943839,35690240,4599894016,128.8838073378044
```
Columns per the design doc: epoch, object_count, ceiling_bytes(ram), bytes_per_key. Object count stable (35.78M → 35.67M → 35.69M, <1% drift). Ceiling still keyed off `ram` (matches #346's known constraint — zone is provisioned bigger than the host has RAM for, so the alarm measures live RAM, not configured zone size). `bytes_per_key` 128.5 → 128.9 → 128.9, consistent with the doc's measured 128.5 (not the 146 design estimate).

### cache-catcher process/log health
`docker ps` on NAS: `cache-catcher   Up 3 days   cache-catcher:guard` — running.
Log line confirms both threads alive: `2026-09-18T22:37:02+0000 MONITOR threads started (liveness 900s, key-budget 86400s)`. Subsequent `KEY-BUDGET up:` and periodic `WRITES`/`DEL` lines through 2026-09-21T22:00:15 show the process actively processing fanotify events, not stalled.

### 2. Migration 0018 deploy state

```
=== schema_migrations, highest 3 ===
(17, '0017_sweep_pass', '2026-09-15 03:03:51')
(16, '0016_commanded_transitions', '2026-09-14 19:19:20')
(15, '0015_games_measurement_split', '2026-09-08 11:20:55')
```
0018 is **not** applied — matches "merged in git, deliberately not deployed."

```
=== status='failed' count ===
19
```
Matches expected 19.

```
=== rows with last_job_outcome NOT NULL (all of them) ===
(15495, 'failed', None, '2026-09-21 19:42:56', 'prefill: EpicManifestError: epic manifest API failed: HTTP 404', '2026-09-21 20:32:20')
```
Exactly 1 row, game **15495** (`epic`, app_id `7e874dd2a7a941eabdf72755b42405db`, title `CHUCHEL`):
- `last_job_outcome` populated at **2026-09-21 20:32:20** — matches.
- `status` = `'failed'` — matches.
- `status_measured_at` = `None` (NULL) — matches.

**#317 verification holds exactly as specified.**

Full status distribution across owned games:
```
('up_to_date', 1842)
('not_downloaded', 1355)
('failed', 19)
('unknown', 5)
('validation_failed', 1)
```
Total 3222. (Note: CLAUDE.md's 2026-09-11 snapshot said 1835 up_to_date / 1355 not_downloaded / 19 failed — up_to_date has grown to 1842 since, consistent with ongoing convergence, not a discrepancy.)

### General live health

**Sweeps, last 7 days** (`jobs` table, `kind='sweep'`, `started_at >= now-7d`):
```
running: 1
succeeded: 26
```
Zero failed. Raw rows (id, state, source, started_at, finished_at) — 26 succeeded 2026-09-15 through 2026-09-21, 1 currently running (job 46654, started 2026-09-21 21:00:00, source=scheduler).

**Current sweep pass:**
```
sweep_pass: (id=1, pass_number=8, pass_started_at='2026-09-21 09:39:27')
```
Pass 8, started today 09:39:27 UTC — consistent with the ~16.4h/3-run cadence in CLAUDE.md (prior pass rolled at job 46647's finish, 09-21 09:39:27, matching sweep_pass exactly).

**Downward measurement transitions, last 24h:** 0 rows (query with `downward=1 AND occurred_at >= now-1d` returned empty). Total transitions in 24h: 1801, all shown non-downward in the most-recent-10 sample (`up_to_date -> up_to_date`, `downward=0`).

**Circuit breaker:** No `breaker`/`circuit`/`trip` log lines anywhere in the container's full retained log (back to container start 2026-09-16 02:34:36). `ORCH_MEASUREMENT_BREAKER_THRESHOLD` is unset in the container env — confirms code default (25) is in effect, no temporary override left behind.

**Agent health:**
```
docker exec orchestrator-agent python3 -c "import os; print(os.getuid())"  -> 0
docker exec orchestrator-agent sh -c "ls /data/cache/cache | wc -l"        -> 256
```
uid 0, 256/256 buckets visible — matches the documented root-UID requirement.

**Host memory (NAS, `free -h`):**
```
               total        used        free      shared  buff/cache   available
Mem:            15Gi       6.5Gi       159Mi       4.7Gi        13Gi       8.9Gi
Swap:             0B          0B          0B
```
159Mi "free" looks alarming in isolation but 8.9Gi is "available" (buff/cache is reclaimable) — normal for a Linux file-cache-heavy host, not a memory-pressure symptom by itself.

**Container memory (`docker stats --no-stream`):**
```
CONTAINER             MEM USAGE / LIMIT     MEM %
lancache-monolithic   4.703GiB / 15.4GiB    30.54%
orchestrator-agent    3.503GiB / 8GiB       43.79%
cache-catcher         21.11MiB / 15.4GiB    0.13%
lancache-dns          50.75MiB / 15.4GiB    0.32%
```
lancache-monolithic well under its 15.4GiB cap — no sign of the #346 host-OOM risk materializing right now (that risk is about the *zone being provisioned* beyond available RAM if it ever fills, not current live usage).

### Container status (all hosts)
```
LXC 10.100.23.105: orchestrator            Up 5 days (healthy)
NAS 192.168.1.30:  orchestrator-agent      Up 4 days (healthy)
                    lancache-dns            Up 2 weeks
                    lancache-monolithic     Up 2 weeks
                    cache-catcher           Up 3 days
```

### Out-of-scope anomaly (flagged, not investigated further)
Kuma monitor 181 `job:orch-epic-prefill` has been continuously DOWN since 2026-09-16 (10 consecutive "No heartbeat in the time window" beats through 2026-09-21 10:37:59). Cross-checked against the orchestrator DB: no `source='scheduler'` Epic prefill jobs at all since 2026-09-01 (that day's queued jobs were explicitly cancelled: `"cancelled 2026-09-01: queued by blind validate (agent could not traverse cache after ugacl removal)"`). The only recent Epic prefill job is 46651, the manual/API-triggered #317 test case. This may be correct behavior (a no-candidates scheduled cycle might not emit a job row or a heartbeat by design) but I cannot confirm that from live data alone — recommend a code-level check of the scheduled-prefill trigger path before the next session closes this as expected. Reported as SEV-2 pending that confirmation, not as a proven bug.

## Commands run (read-only)
All queries went through local Python scripts written to the session scratchpad, `scp`'d to `/tmp` on the target host, `docker cp`'d into the container where relevant, and run via `docker exec <container> python3 /tmp/<script>.py` opening the DB with `mode=ro`. No `INSERT`/`UPDATE`/`DELETE` statements were issued. No container was restarted, recreated, or had a job triggered against it.
