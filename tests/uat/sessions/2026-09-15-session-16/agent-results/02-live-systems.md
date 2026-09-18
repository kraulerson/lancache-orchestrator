# UAT Session 16 — Live Systems Audit (Read-Only)

**Role:** Release Engineer / SRE — Phase 4.1 mindset ("prove rollback works, prove monitoring catches failures, assume nothing")
**Date:** 2026-09-15, audit window ~19:30–19:50 UTC
**Scope:** Control plane (`root@10.100.23.105`, container `orchestrator`) + data plane (`karl@192.168.1.30`, NAS)
**Method:** SQLite read-only (`?mode=ro`), `docker logs`/`ps`/`inspect`, health endpoint. No writes, no restarts, no job triggers. The live sweep (job 46576) was not disturbed.

---

## Summary

The #311/#322 sweep pass-marker fix is **working** — the first sweep since deploy (job 46573) `succeeded` in 5h37m, the second is in progress and on pace, and the candidate count shrank by exactly the number attempted (3213 → 2789 = 424 attempted). That reverses a baseline of 20 failures in the 21 sweeps run between the 2026-09-08 measurement-split deploy and this fix.

However, `sweep_pass.pass_number` is still `1` and has **never incremented** — `sweep.pass_completed` has fired **zero** times in the entire log history, not just since deploy. The "pass" concept has not yet been proven end-to-end; only "a single sweep run no longer gets killed" has been proven.

Two things contradict what the docs/CLAUDE.md claim as current state:
- `ORCH_SWEEP_BATCH_SIZE` is baked into the running container as **`2`**, not the `4` that CLAUDE.md says was set 2026-09-11 and still in effect.
- The measurement-before-download gate is far more restrictive in practice than "1355 not_downloaded, ready to convergence" suggests: only **5 of 3213** owned games currently have both an eligible status and a non-NULL `status_measured_at` (1 `validation_failed`, 4 `not_downloaded`). The other 1351 `not_downloaded` games have never been measured under the post-0015 single-writer path and are therefore **not eligible** for Epic scheduled prefill regardless of the pass-marker fix.

`last_job_outcome` (#317) is NULL on **100% of owned games (3213/3213)** — the coverage gap is total, not partial. Purge (#321) has not run since 2026-08-25, confirming it's unexercised since the #321 change landed. No jobs are stuck; the agent is healthy, uid 0, well under its memory limit, and hasn't been recreated in 6 days.

---

## Findings

### Finding 1 — SEV-3 (informational, positive): Sweep pass-marker fix demonstrably fixes the runtime-cap failure mode
**Evidence:**
- Baseline (2026-09-08 11:22, the 0015 deploy, through 2026-09-15 03:03:51, the 0017 deploy): **20 of 21 sweep jobs failed** — 12 killed by "orchestrator restarted while job was running (ID6 reaper)", 8 by "job exceeded max runtime of 21600.0s (cancelled)". Only job 46504 succeeded in that window.
- Post-fix: job 46573 (`2026-09-15 09:00:00` → `14:36:58`) `state='succeeded'`, `error=NULL`, elapsed 20,218,259 ms (5h37m), inside the 21,600s budget.
- Log line at completion:
  `{"job_id": 46573, "total": 3213, "cached": 214, "validation_failed": 0, "validation_error": 210, "evicted": 0, "recovered": 0, "errors": 0, "pass_number": 1, "attempted": 424, "attempted_bytes": 4159015713622, "remaining_bytes": 4727692976275, "event": "sweep.pass_progressed", ... "timestamp": "2026-09-15T14:36:58.870209Z"}`
  `{"kind": "sweep", "job_id": 46573, "elapsed_ms": 20218259, "event": "jobs.handler.completed", ...}`
- Job 46576 started `2026-09-15 15:00:00` with `candidates=2789` and `remaining_bytes=4727692976275` — **exactly** matching the prior run's `remaining_bytes`, and `3213 - 424 = 2789` **exactly** matches the candidate shrink claim in the audit brief.
- Job 46576 is still `running` at audit time, 16,885s (4h41m) elapsed of the 21,600s budget — on pace to finish inside budget, not yet stuck.

**Why it matters:** this is the first hard evidence since the measurement-split (0015, 2026-09-08) that a sweep can complete without being killed. That was previously true in only 1 of 21 runs (4.8%).

### Finding 2 — SEV-2: `sweep.pass_completed` has never fired; `pass_number` has never incremented, ever
**Evidence:**
```sql
SELECT * FROM sweep_pass;
-- (1, 1, '2026-09-15 03:03:51')  -- one row, pass_number=1, stamped at migration time
```
`docker logs orchestrator --since 2026-09-14T00:00:00 2>&1 | grep -c 'sweep.pass_completed'` → **0**
`docker logs orchestrator --since 2026-09-15T03:00:00 2>&1 | grep -c 'sweep.aborted'` → **0**
Both post-deploy runs (46573, 46576) logged `pass_number: 1` at `sweep.started` — unchanged from the seed value.

**Why it matters:** the #311/#322 design's actual goal — a sweep *pass* that completes and rolls to the next pass, restamping `pass_started_at` and giving Kuma's sweep-completion heartbeat something to fire on — has not been observed even once. What's proven so far is narrower: individual sweep *jobs* no longer get killed by the runtime cap. At the current attempt rate (~424–515 games per ~5.5–6h run) it will take roughly 6–7 more runs (~40+ hours) to exhaust the current ~2789 remaining candidates and reach `sweep.pass_completed` for the first time. Do not report the pass-marker feature as fully proven until that event is observed.

### Finding 3 — SEV-2: Measurement-before-download gate blocks nearly the entire `not_downloaded` backlog
**Evidence:**
```sql
SELECT status, COUNT(*) FROM games WHERE owned=1 GROUP BY status;
-- up_to_date        1836
-- not_downloaded     1355
-- failed               19
-- unknown               2
-- validation_failed     1

SELECT status, COUNT(*) FROM games WHERE owned=1 AND status_measured_at IS NULL GROUP BY status;
-- failed            19
-- not_downloaded  1351
-- unknown            2
```
Only **4 of 1355** `not_downloaded` games and the **1** `validation_failed` game have a non-NULL `status_measured_at` — i.e. only 5 owned games currently satisfy Epic scheduled prefill's gate (`status IN ('validation_failed','not_downloaded') AND status_measured_at IS NOT NULL`), per `docs/superpowers/specs/2026-09-04-cache-validation-integrity-design.md`. Of those 4 `not_downloaded` rows, 3 were last measured **2026-06-18** (pre-dating the 0015 split entirely) and only 1 (`game_id 1171`) was measured today (2026-09-15 17:53:22).

Cross-check — `last_measure_attempt_at` (the *attempt* timestamp, written on every sweep touch regardless of outcome) is populated on all 3213 owned rows and spreads plausibly across real sweep windows (827 rows at `2026-09-14 23:xx`, 515 at `2026-09-15 15:xx`, etc. — not a bulk backfill artifact). So games are being *attempted*, but the vast majority of `not_downloaded` games are not completing a `record_measurement()` write that would stamp `status_measured_at`.

**Why it matters:** this explains the "three most recent prefill cycles each enqueued 0 downloads" note in CLAUDE.md far more precisely than "convergence is done" — for all but a handful of games, the code-level precondition for Epic prefill literally cannot be met yet. This is a correctness/progress concern the current CLAUDE.md summary does not capture accurately; flag before claiming convergence is meaningfully complete.

### Finding 4 — SEV-2: #317 `last_job_outcome` gap is total, not partial
**Evidence:**
```sql
SELECT COUNT(*) FROM games WHERE owned=1;                          -- 3213
SELECT last_job_outcome, COUNT(*) FROM games WHERE owned=1 GROUP BY last_job_outcome;
-- (None, 3213)
SELECT MAX(last_job_outcome_at) FROM games;                        -- NULL
```
**Why it matters:** confirms #317 as filed, with hard numbers — `record_job_outcome()` has never successfully written to a single owned game's row. 0/3213, not "mostly NULL."

### Finding 5 — SEV-3: Live `ORCH_SWEEP_BATCH_SIZE` contradicts CLAUDE.md
**Evidence:**
```
$ ssh root@10.100.23.105 grep -iE 'sweep|batch' /root/orch-lxc.env
ORCH_SWEEP_BATCH_SIZE=2
$ docker inspect orchestrator --format '{{range .Config.Env}}{{println .}}{{end}}' | grep BATCH
ORCH_SWEEP_BATCH_SIZE=2
```
CLAUDE.md's Current State section says: *"`ORCH_SWEEP_BATCH_SIZE` was raised 2 → 4 on 2026-09-11 to bring a pass inside the cap."* The env file backing the running container has it at **2**, not 4. Both the source file on disk and the value actually baked into the container's `Config.Env` agree with each other — so this isn't a stale-inspect artifact, the raise to 4 is simply not in effect right now (either reverted, or never actually deployed).

**Why it matters:** doesn't appear to be causing active harm — job 46573 completed in 5h37m at batch size 2 — but it means the documented remediation history is wrong, and if this value is relied on by anyone reasoning about pass-completion timing, they'd expect faster progress than what's actually happening.

### Finding 6 — SEV-4 (confirms prior belief): purge (#321) unexercised since 2026-08-25
**Evidence:**
```sql
SELECT COUNT(*) FROM jobs WHERE kind='purge';                      -- 6, all time
SELECT id, state, source, started_at, finished_at FROM jobs WHERE kind='purge' ORDER BY id DESC LIMIT 10;
-- 45142  succeeded  api  2026-08-25 12:44:28  2026-08-25 12:44:28
-- 45139  succeeded  api  2026-08-25 12:39:32  2026-08-25 12:39:33
-- 45135  succeeded  api  2026-08-25 12:38:25  2026-08-25 12:38:26
-- 45134  succeeded  api  2026-08-25 12:36:52  2026-08-25 12:37:16
-- 45130  succeeded  api  2026-08-25 12:34:37  2026-08-25 12:34:47
-- 36079  succeeded  api  2026-07-05 18:49:23  2026-07-05 18:49:34
```
No purge job has run since 2026-08-25 — **weeks before** #321 (2026-09-14) landed. Confirmed: the #321 delete-then-validate path has never been exercised live.

**Why it matters:** matches the stated belief exactly. Anything claimed about #321's live behavior is unverified — it's only ever run through whatever pre-#321 code path existed on 2026-08-25 and earlier.

---

## Verified healthy

- **Migration state:** `schema_migrations` shows `0017_sweep_pass` applied `2026-09-15 03:03:51`, immediately following `0016_commanded_transitions` (`2026-09-14 19:19:20`) and `0015_games_measurement_split` (`2026-09-08 11:20:55`) — sequential, no gaps, no skipped IDs.
- **Deployed image contains the pass-marker code:** `from orchestrator.jobs.sweep_pass import read_pass` succeeds inside the running container (`<function read_pass at 0x778662f81760>`).
- **`sweep_deadline_margin_sec` default confirmed at 1800.0:** `orchestrator/core/settings.py:306: sweep_deadline_margin_sec: float = Field(default=1800.0, ge=0.0)`, present in both the installed site-packages copy and `/app/src`, and no env override exists in `/root/orch-lxc.env`.
- **Container deploy timing is self-consistent:** `docker inspect orchestrator` shows `Created`/`StartedAt` = `2026-09-15T03:03:50Z`, one second before the `0017_sweep_pass` migration timestamp — i.e., migrations ran on container start, as expected, not applied out-of-band.
- **Health endpoint green:** `{"status":"ok","scheduler_running":true,"lancache_reachable":true,"cache_volume_mounted":true,"validator_healthy":true,"steam_auth_ok":true,"agent_reachable":true}`, `uptime_sec=59913` (matches the 03:03 start).
- **No jobs stuck beyond budget.** Only one job is `running` right now (46576, the live sweep, 4h41m into a 6h budget — not overdue). No other kind is running or orphaned.
- **Job mix since 2026-09-14 is otherwise clean:** `fetch_manifests` (1, succeeded), `library_sync` (10, succeeded, avg ~27s), `sweep` (2 failed [pre-fix], 1 succeeded, 1 running). No `prefill` or `validate` jobs ran in this window — consistent with almost nothing being prefill-eligible (Finding 3).
- **12 reaper-cancelled sweeps since the 0015 deploy (2026-09-08)** — all pre-date the 0017 fix; none since.
- **Agent (NAS, `192.168.1.30`):** `orchestrator-agent` up 6 days, healthy, `User: 0:0` (correct — uid 0 required, uid 1000 causes the 65/256-bucket false negative), `RestartCount: 0`, memory `982.5MiB / 8GiB` (12%) — comfortably under the 8g limit, no recent recreate.
- **Other NAS containers:** `lancache-monolithic` up 2 weeks (5.98GiB/15.4GiB, 38.9%), `lancache-dns` up 2 weeks (65MiB), `cache-catcher` up 2 weeks (10.7MiB) — all stable, no restarts observed.

---

## Recommendation for the gate

Do not report the #311/#322 fix as fully validated — only the runtime-cap symptom is fixed (Finding 1). The actual "pass" semantics (Finding 2) need at least one observed `sweep.pass_completed` before sign-off. Separately, flag Finding 3 to whoever owns the "convergence is done" narrative — it is optimistic given the measured-games count, independent of the pass-marker work. Finding 5 (batch size drift) should be reconciled between CLAUDE.md and the live env file regardless of severity.
