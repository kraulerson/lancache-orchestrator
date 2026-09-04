# Cache Validation Integrity — Design

**Date:** 2026-09-04
**Status:** Approved (Orchestrator: Karl Raulerson)
**Supersedes assumption:** the handover recommendation of a persisted resume cursor

## Problem

Three confirmed defects, all observed live on 2026-09-01.

### D1 — The sweep can never finish

`jobs/handlers/sweep.py:30-39` selects candidates with `ORDER BY id` and no
cursor, offset, or resume predicate. `sweep.py:66` calls `read_all()` and walks
from the top. Job 46446 ran for exactly its 21600s budget against 3197
candidates, was cancelled, and moved `steam.up_to_date` from 6 to 46. The next
run restarts at the same low ids. Games past the six-hour mark are unreachable
permanently, not intermittently.

### D2 — `not_downloaded` is a dead end

The gated candidate SQL filters `status IN ('unknown','up_to_date','validation_failed')`.
`not_downloaded` is absent, so 1357 Steam games stamped on 2026-06-18 have been
invisible to every sweep since. No amount of running the sweep reaches them.

### D3 — Job outcomes are written into cache truth

`games.status` carries two unrelated meanings: what a measurement found on disk,
and how the last job ended. On 2026-09-01 between 03:00 and 03:47 an orchestrator
restart interrupted a prefill batch and stamped **1769 games** (1115 Steam, 654
Epic) with the dead job's outcome.

The dominant recorded reason was
`prefill interrupted (orchestrator restart or job timeout) — re-run prefill`
(350 rows). A further 83 rows carry no error at all. Only ~81 were genuine Epic
API failures (16 × HTTP 404 on dead titles, 58 × HTTP 403 transient, 7 × bad
manifest magic).

Epic's scheduled prefill (`scheduler/jobs.py:165`) selects on
`g.status <> 'up_to_date'`, so those corrupted statuses would have queued **655
Epic prefills** — every Epic game not proven cached — re-downloading titles that
are almost certainly already on disk.

Steam is structurally protected from that specific outcome: the same query is
hard-scoped to `g.platform = 'epic'`, and Steam's selection logic keys on
`last_prefilled_at` and the on-disk `.bin` cache, not on status. Steam still
suffers the corrupted records (wrong UI badges) and D2.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Resumable measurement; accept it takes days | Throughput may be disk-bound; correctness first |
| 2 | Split cache truth from job outcome into separate columns | Makes D3 structurally impossible |
| 3 | Never download without positive evidence of absence | "Unknown" triggers measurement, never a download |
| 4 | Both a build-breaking code guard and a live alarm | Code guard stops regression; alarm catches operational surprise |

## Design

### Data model (migration 0015)

| Column | Meaning | Permitted writer |
|---|---|---|
| `status` | Cache truth: `cached` / `partial` / `missing` / `unknown` | `record_measurement()` **only** |
| `status_measured_at` *(new)* | When cache truth was last established by a real measurement | `record_measurement()` only, on success |
| `last_measure_attempt_at` *(new)* | When measurement was last **attempted**, success or failure | Every attempt |
| `last_job_outcome` *(new)* | How the last job ended | Any job, any path |
| `last_job_outcome_at` *(new)* | When that job ended | Any job, any path |

`last_error` is renamed to `last_job_outcome`. It is never consulted for
download decisions.

`not_downloaded` folds into `missing` — it is a genuine measurement result
(the checker looked and found nothing) and remains cache truth.

**Repair.** Migration 0015 resets the corrupted rows to `status = 'unknown'`,
`status_measured_at = NULL`. Under decision 3, `unknown` means *measure it*,
never *download it*, so the reset starts no traffic.

The predicate must be exact, because legitimately-cached rows also fall inside
the incident window (Epic `up_to_date` runs from 03:30:09; Steam `up_to_date`
to 23:28:13). Resetting those would discard good measurements for no gain:

```sql
UPDATE games SET status = 'unknown', status_measured_at = NULL
WHERE status IN ('validation_failed', 'failed')
  AND last_validated_at >= '2026-09-01 03:00:00'
  AND last_validated_at <= '2026-09-01 03:47:59';
```

`up_to_date` rows inside the window are left untouched. The 1357 Steam
`not_downloaded` rows from 2026-06-18 are **not** reset either — they are genuine
measurements, merely stale, and fold into `missing`. Their old timestamps put
them at the front of the measurement queue naturally.

**Backfill of `last_measure_attempt_at`.** Seed from `last_validated_at` rather
than NULL, so the June rows sort ahead of the September ones instead of the
whole library tying at the front in arbitrary id order:

```sql
UPDATE games SET last_measure_attempt_at = last_validated_at;
```

### Measurement scheduling — ordering, not a cursor

A stored resume cursor goes stale on every insert or delete and needs its own
repair path. Ordering achieves the same result with no persisted state:

```sql
SELECT id FROM games
WHERE owned = 1
ORDER BY last_measure_attempt_at ASC NULLS FIRST
```

Each run takes whatever has waited longest. Interrupt at any point and the next
run continues correctly, because touched games sort to the back.

`last_measure_attempt_at` is stamped on **every** attempt. Without it a game that
always times out sorts to the front forever and blocks the queue
(head-of-line blocking). `status_measured_at` moves **only on success**.

- Attempt fails → attempt timestamp moves, cache truth untouched, game rotates back
- Attempt succeeds → both move, truth updated

**Note the absence of any `WHERE status IN (...)` filter.** That filter is D2.
Removing status from candidate selection means no status value can ever be
excluded again — this removes the bug class, not just the instance.

Requires an index on `last_measure_attempt_at`.

### Timeout behaviour

A sweep hitting its runtime budget writes **zero** cache truth; it leaves only
attempt timestamps. Replayed against this design, the 2026-09-01 event would
have corrupted nothing.

Per-game timeouts scale with chunk count. The current fixed 300s client timeout
cannot ever succeed for ARK ModKit, which needs ~433s measured.

### Download policy

```sql
-- Epic scheduled prefill, new form
WHERE g.owned = 1 AND g.platform = 'epic'
  AND g.status IN ('missing','partial')
  AND g.status_measured_at IS NOT NULL
```

Previously `g.status <> 'up_to_date'`, which is why "unknown" meant "download it".
Nothing downloads without a measurement that actually found it absent.

### Prevention — code guard

All cache-truth writes funnel through a single function, `record_measurement()`,
the only place permitted to write `status` and `status_measured_at`. A test scans
the source and **fails the build** if any other module issues an
`UPDATE games SET ... status`. Job outcomes write via a separate path with no
access to truth columns.

### Prevention — live alarm (circuit breaker)

Inside `record_measurement()`. States are ranked:

| State | Rank |
|---|---|
| `cached` | 3 |
| `partial` | 2 |
| `missing` | 1 |
| `unknown` | 0 |

**Counted:** any transition to a lower rank — cached→partial, cached→missing,
cached→unknown, partial→missing, partial→unknown.
**Ignored:** any upward or level transition — unknown→anything, missing→cached,
partial→cached, no-change.

Ignoring upward moves is what keeps the recovery sweep silent: all 1769 repaired
games start at `unknown`, so every measurement of them moves up.

**Threshold: 25 downward transitions within 60 minutes** → stop writing and email.
Reuses the cache-catcher email alarm already running on the NAS
(`/log/alert.env`), not a second notification path.

`failed` is no longer a cache state — it lives in `last_job_outcome` and can
never reach the alarm. A crashed job produces no state transition at all.

**Migration 0015 is exempt by construction:** it writes directly rather than
through `record_measurement()`, so its mass downward reset cannot trip the
breaker on its own repair.

## Testing (test-first)

- An interrupted sweep writes zero cache truth — the 2026-09-01 replay
- A per-game timeout moves `last_measure_attempt_at` only, never `status`
- Games with `missing` status appear as candidates — the D2 regression
- A repeatedly-failing game rotates to the back and does not block the queue
- Epic prefill queues nothing for `unknown` games
- Migration 0015 resets exactly the damaged rows and no others
- The circuit breaker trips at 25 downward transitions and holds
- The circuit breaker ignores upward transitions at any volume
- The source-scan guard fails the build on an out-of-band `status` write

## Rollout

1. Build test-first, tests green, PR, CI green — Orchestrator merges
2. Deploy to LXC 1105 with a `dpa-pre-N` rollback tag on both hosts
3. **Measure** — sweep runs for days across ~3200 games
4. **Then** re-enable Epic downloading, which fetches only what measurement
   proved absent

Step 4 is where "download what's remaining" happens, against a real number
rather than 655 guesses.

Prefill is already disarmed: `/root/orch-lxc.env` was set to
`ORCH_SCHEDULED_PREFILL_ENABLED=false` on 2026-09-04 (backup:
`/root/orch-lxc.env.bak-20260904`). Previously the env file said `true` and only
a hand-typed `-e` override on a one-off `docker run` held it off — meaning the
next run of `/root/deploy-orchestrator-lxc.sh`, which passes only
`--env-file`, would have re-armed all 655.

## Out of scope

- Steam download behaviour — stays driven by the host SteamPrefill cron
- Agent throughput / NAS disk performance — measured separately if the sweep
  proves too slow once resumable
- Game_shelf UI changes — separate repo; tracked as follow-up (see below)

## Coupled follow-up (separate repo: Game_shelf)

- **Remove `Failed` from the cache-status filter.** `failed` ceases to be a cache
  state when this ships, so the option becomes dead. Must land *after* the
  orchestrator deploy.
- **Humble Bundle missing from filter options.** Independent of this work.
  Orchestrator-side coverage is live (17/18); GS #22 remediation is already open
  and may be the cause.
