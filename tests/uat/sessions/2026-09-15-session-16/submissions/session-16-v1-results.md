# UAT Session 16 — results

**Tester:** Claude (executed on the Orchestrator's instruction — "Run the UAT
yourself. I currently don't have time.", 2026-09-16)
**Date executed:** 2026-09-16
**Template:** `templates/test-session-16-v1.html`

> **Provenance caveat, stated plainly.** These scenarios were written for a human
> and I executed them myself, which means the same agent wrote the checks and
> graded them. Where a scenario expected a human to *look at something*, I read
> the underlying data store instead (Kuma's SQLite DB rather than its web UI).
> That is stronger evidence in most cases, but it is not independent. Scenario 3
> in particular ("do the badges match reality?") is only half-tested: I verified
> what the database says, not what a human sees in Game_shelf.

## Scenario results

| # | Feature | Scenario | Result |
|---|---|---|---|
| 1 | 26 | Kuma sweep monitor 180 is green | **PASS** |
| 2 | 26 | Cut-off sweep recorded succeeded | **PASS** |
| 3 | 26 | Cache badges trustworthy | **PARTIAL** — DB verified, UI not viewed |
| 4 | 26 | No game starved | **PASS** |
| 5 | 27 | Kuma breaker monitor 216 green | **PASS** |
| 6 | 27 | Decide whether PR #327 ships | **PASS** — merged + deployed 02:33 UTC |
| 7 | 18 | Purge a disposable game | **PASS** (resolved 2026-09-16 19:24) |
| 8 | 18 | Purge did not arm the breaker | **PASS** (resolved 2026-09-16 19:24) |
| 9 | 18 | Purged game re-prefills | **FAIL** — see #339, SEV-2 |

**9 of 9 attempted, 7 passes, 1 partial, 1 FAIL.**

### 1 — Sweep monitor 180: PASS

Read from Kuma's DB directly (`/opt/uptime-kuma/data/kuma.db`):

```
2026-09-16 14:32  UP  pass 2 partial: 1310/2890 games, 3.5 TiB of 4.4 TiB
2026-09-16 08:31  UP  pass 2 partial: 327/3216 games, 3.7 TiB of 8.1 TiB
2026-09-16 01:22  UP  pass 1 complete: 1469 games, 917.1 GiB
2026-09-15 20:30  UP  pass 1 partial: 1323/2789 games, 3.4 TiB of 4.3 TiB
```

Green, with the partial/complete distinction legible exactly as designed. This
monitor could not go green at all before #322.

### 2 — Sweeps succeed: PASS

Since 2026-09-15: **5 succeeded, 1 running, 0 failed.** Baseline was 15 of 16
failed.

### 3 — Badges: PARTIAL

`Mad Max`, `Suicide Squad: Kill the Justice League`, `Team Fortress 2` and
`Counter-Strike 2` all `up_to_date` with measurements inside 24h. Mad Max and
Suicide Squad were independently corroborated by a real client download pulling
12,850 HITs / 0 MISSes from lancache. **Not tested: the Game_shelf UI itself.**

### 4 — No starvation: PASS

Oldest `last_measure_attempt_at` 2026-09-15 20:15:48, newest 2026-09-16
15:14:53, **zero NULLs** across 3217 owned games.

Caveat worth recording: oldest `status_measured_at` is **2026-06-18** — a game
still carrying three-month-old cache truth because every attempt since has
returned `error`. Attempt freshness is not measurement freshness. See Finding 1.

### 5 — Breaker monitor 216: PASS

Green; daily `breaker armed daily heartbeat` at 12:17 on 13, 14, 15 and 16 Sep.

### 6 — PR #327: PASS

Decision taken and executed: merged, then deployed at 02:33 UTC in the gap after
sweep 46579 completed. `jobs.reaper.no_orphans` confirms no job was killed.
Verified live in the running container: `push -> bool`, `_BREAKER_RETRY_SEC` 60.0.

### 7 — Purge: PASS (resolved 2026-09-16 19:24:03)

Purge triggered on **Alien Shooter** (id 300, 101 MB, smallest fully-cached Steam
title) via `POST /api/v1/games/300/purge` → job 46589. It sat `queued` for over
five hours behind the running sweep (Finding 3, now #331), then ran in **four
seconds** once the worker freed up.

| evidence | value |
|---|---|
| job 46589 | **succeeded**, 19:23:59 → 19:24:03 |
| status after | **`not_downloaded`** — not `validation_failed` |
| post-purge measurement | 657 chunks total, **0 cached**, outcome `missing` |
| deletes observed by cache-catcher | **583**, matching the 583 chunks it held |

This is the first production exercise of the #310/#321 delete-then-validate path.
It behaved exactly as designed: cache truth came from a real measurement of the
disk, not from the agent's delete counts.

### 8 — Breaker not armed: PASS

The transition row reads `up_to_date → not_downloaded`, `downward = 1`,
**`commanded = 1`**. In the hour that followed: **0** breaker-visible downward
transitions, 1 commanded (exempt), no trip and no refusal logged, Kuma 216 still
green on its daily heartbeat. Migration 0016's `commanded` flag did precisely its
job.

### 9 — Purged game re-prefills: **FAIL** (#339, SEV-2)

It does not. Verified over the following day:

- `2026-09-17 12:29:48` — the host SteamPrefill cron ran and completed OK.
- It **skipped Alien Shooter entirely**: zero depot requests for 33100/33101/33102
  in the lancache access log on 17/Sep; the most recent is `06/Aug/2026`.
- Cause: SteamPrefill's own `successfullyDownloadedDepots.json` (2375 entries)
  still records app 33100 as downloaded. The cache was emptied underneath it.

`handlers/purge.py` claims reversibility via "both in the set F5/F6 select on".
That holds for **Epic**, where the orchestrator owns prefill and selects on
status. It does not hold for **Steam** — 2540 of 3217 owned games — where prefill
is the host cron driven by state the orchestrator never touches on purge.

**Alien Shooter is currently purged and will not return on its own.** That is the
real cost of running this scenario, and it is exactly what the scenario existed to
discover.

## Findings

### Finding 1 — SEV-2 — the measurement gate permanently excludes 42% of the library from prefill

A full pass has now completed, so this is no longer a transient state.

```
owned games                     3217
  status_measured_at SET        1842
  status_measured_at NULL       1375   <- all 1375 HAVE been attempted
prefill-eligible right now         5
```

NULL group by status: **1351 `not_downloaded`**, 19 `failed`, 5 `unknown`.

The Epic scheduled-prefill gate requires
`status IN ('validation_failed','not_downloaded') AND status_measured_at IS NOT NULL`.
A game with no manifest returns `validation_error`, which by design writes no
cache truth, so `status_measured_at` stays NULL forever. It therefore never
becomes prefill-eligible — and never being prefilled, it never acquires a
manifest. **Circular deadlock, 1351 of 3217 owned games.**

This is the real explanation for "three prefill cycles enqueued 0 downloads",
which CLAUDE.md attributes to convergence being complete. Convergence is not
complete; 42% of the library is structurally unreachable.

### Finding 2 — SEV-3 — Epic prefill monitor 181 is DOWN and cannot say why

Kuma 181 has been DOWN since at least 2026-09-15 03:08 ("No heartbeat in the time
window"). Cause: **no prefill job has been created since 2026-09-01.**
`enqueue_scheduled_prefill` is a bulk `INSERT INTO jobs ... SELECT`; with zero
eligible rows it inserts nothing, so no job runs, so nothing pushes a heartbeat.

The monitor cannot distinguish "there was nothing to prefill" from "the scheduler
is dead" — the same defect class as #326, on a different monitor. Downstream of
Finding 1.

### Finding 3 — SEV-3 — an operator purge can wait hours behind a sweep

The single-loop worker (spec D10) runs one job at a time. A sweep occupies it for
up to its full 5.5h budget, and an operator-triggered purge queues behind it.
From Game_shelf the button appears to do nothing, with no queue-position feedback.
Observed directly: job 46589 still `queued` 150s after a 200-response trigger.

### Findings carried from the agent phase (unchanged, now filed)

- **SEV-2** purge coerces agent counts with `int()` *before* the post-delete
  measurement, so a malformed response strands a stale false-green — verified by
  hand at `handlers/purge.py:142-178`.
- **SEV-3** `get_settings()` unprotected in the breaker path; latent only.
- **SEV-3** `_BREAKER_RETRY_SEC` never proven to bound anything; TOCTOU race
  untested at real concurrency; deadline ordering only proven at batch size 1.
- **SEV-2 (process)** my own stale venv meant three "1912 passed" claims never
  exercised the merged dependency bumps. Fixed and re-verified.

## Overall

**Updated 2026-09-17** after the blocked scenarios resolved.

The two features under test both work. #322 is proven end to end in production —
a pass completed, the arithmetic closes, the monitor is green, zero evictions.
#313 is deployed and verified present, though still unexercised because the
breaker has not tripped.

The session's real value was elsewhere: Finding 1 is a bigger problem than either
feature it was meant to test, and it was invisible until a full pass finished.


## Addendum — 2026-09-17

Scenarios 7 and 8 resolved overnight and both PASS: the purge path is now proven
in production for the first time, and the `commanded` flag kept the breaker
quiet as designed.

Scenario 9 FAILED, and it is the most valuable result in this session. A purged
**Steam** game never re-downloads, because SteamPrefill still records it as
already fetched. Filed as **#339 (SEV-2)**. It was invisible until now precisely
because the purge path had never run in production — which is the argument for
running scenarios that look like formalities.

One further finding came out of the purge landing: cache-catcher emailed an
eviction alarm for the commanded delete, having logged the responsible process on
every line and ignored it. Filed as #337, fixed, deployed and verified the same
day.
