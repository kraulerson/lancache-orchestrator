# F13 — Scheduled Validation Sweep — Design

**Date:** 2026-06-06
**Feature:** F13 (PRODUCT_MANIFESTO §52, OQ7) — *Scheduled Full-Library Validation Sweep.*
**Status:** Approved design → implementation plan next.

## Goal

A scheduled (default weekly, Sundays 03:00 UTC) job that re-runs F7 disk-stat validation across the **cached** Steam library to catch **LRU eviction drift** — games that were `up_to_date` but got evicted flip to `validation_failed`, and games that were `validation_failed` but got re-cached flip back to `up_to_date`. It keeps `games.status` honest over time without operator action (Manifesto journey §96 "steady state"; §117 X5 post-sweep panic is a separate status-page concern).

## Decisions (locked)

- **Inline batch in one sweep job.** A single `sweep` job validates the candidate games itself, 10 at a time, rather than enqueuing ~N individual `validate` jobs. Matches the Manifesto's "in batches of 10" + "per-game errors don't abort the batch", produces one tidy job with a summary, and doesn't flood `/jobs` with thousands of rows weekly.
- **Candidate set = Steam games with `status IN ('up_to_date','validation_failed')`.** Catches drift both ways (eviction *and* recovery).
- **Steam-only.** `validate_handler`/`validate_game` are steam-only; Epic disk-stat validation (F7-Epic) is deferred, so Epic games are not swept.
- **Manifest pruning deferred.** The `0001` "prune latest-3 versions during F13 sweep" comment is a separable concern; out of scope for this cut (follow-up).
- **No new table.** Per-game results already persist in `validation_history` + `games.status`; the sweep emits a structured summary log. A `sweep_runs` table is unnecessary for MVP.

## Architecture

Follows the established pattern (F12 D6): **the scheduler enqueues, the jobs worker executes.** The cron fires a thin, never-raises callback that inserts one `sweep` job row; the jobs worker claims it and runs the sweep handler inline.

### Components

**1. Settings** (`core/settings.py`)
- `validation_sweep_enabled: bool = True`
- `validation_sweep_cron: str = "0 3 * * 0"` — standard 5-field cron (min hour dom mon dow), UTC; validated by constructing an APScheduler `CronTrigger.from_crontab(...)` at settings-validation time (fail-fast on a bad expression, per IS2).
- `sweep_batch_size: int = Field(default=10, ge=1)`

**2. Migration `0005_jobs_sweep_unique.sql`** — DB-enforced single-in-flight sweep, mirroring `idx_jobs_library_sync_inflight`:
```sql
CREATE UNIQUE INDEX idx_jobs_sweep_inflight ON jobs(kind)
    WHERE kind = 'sweep' AND state IN ('queued','running');
```
(One row max in the partial set → at most one queued/running sweep.) `CHECKSUMS` updated with the new file's SHA-256.

**3. Scheduler cron** (`scheduler/manager.py`)
- New stable job id `VALIDATION_SWEEP_JOB_ID = "validation_sweep"`.
- In `start()`, when `validation_sweep_enabled`, register a second job: `enqueue_validation_sweep` on `CronTrigger.from_crontab(validation_sweep_cron, timezone="UTC")`, `replace_existing=True`, same `job_defaults` (max_instances=1, misfire_grace_time=None, coalesce=True).
- `SchedulerManager.__init__` gains `validation_sweep_enabled: bool` + `validation_sweep_cron: str`. The library_sync job is unchanged.

**4. Enqueue callback** (`scheduler/jobs.py`)
- `async def enqueue_validation_sweep(pool) -> int` — mirrors `enqueue_library_sync`: `INSERT INTO jobs (kind, state, source) VALUES ('sweep','queued','scheduler') ON CONFLICT DO NOTHING`, returns rowcount, **never raises** (logs `scheduler.sweep.queued` / `.dedup_skip` / errors swallowed). `platform` left NULL (sweep is not platform-scoped).

**5. `validate_one_game` refactor** (`jobs/handlers/validate.py`)
- Extract the per-game core from `validate_handler` into:
  `async def validate_one_game(pool, deps, game_id, settings) -> ValidationResult` — reads the game, runs `validate_game(...)`, inserts the `validation_history` row, and applies the status update (including the UAT-10 #3 transient-`downloading` rule). Returns the `ValidationResult` (with `outcome`).
- `validate_handler` becomes a thin wrapper: platform/steam_client/game_id guards → `await validate_one_game(...)`. Behaviour is unchanged (existing tests stay green).

**6. Sweep handler** (`jobs/handlers/sweep.py`, registered for `kind='sweep'`)
- **Pre-flight (skip, don't fail):**
  - `validator_self_test(settings)` False → log `sweep.skipped reason=validator_unhealthy`; job **succeeds** (nothing to do; not an error).
  - `deps.steam_client is None` → log `sweep.skipped reason=no_steam_client`; succeed.
- **Enumerate:** `SELECT id, status FROM games WHERE platform='steam' AND status IN ('up_to_date','validation_failed') ORDER BY id`. The prior `status` is retained per game so the summary can classify eviction vs recovery.
- **Validate in batches of 10:** `asyncio.Semaphore(settings.sweep_batch_size)`; for each game, `validate_one_game(...)` under the semaphore. Each call is wrapped in try/except — a per-game failure is counted + logged (`sweep.game_error game_id=… error=…`) and **never aborts** the sweep.
- **Summary:** tally `validated`, `errors`, and a by-outcome count (`cached`/`partial`/`missing`/`error`) plus `evicted` (prior `up_to_date` → outcome ≠ `cached`) and `recovered` (prior `validation_failed` → outcome `cached`), derived from the retained prior status vs the returned `ValidationResult.outcome`. Emit `sweep.completed total=… cached=… validation_failed=… evicted=… recovered=… errors=…`.

### Data flow
```
CronTrigger (Sun 03:00 UTC)
  └─ enqueue_validation_sweep(pool)  →  INSERT jobs(kind='sweep') ON CONFLICT DO NOTHING
        └─ jobs worker claims the sweep job
              └─ sweep_handler:
                   pre-flight (validator/steam health) ── unhealthy ─→ skip + succeed
                   enumerate up_to_date+validation_failed steam games
                   Semaphore(10) → validate_one_game(g) per game  (errors isolated)
                   emit sweep.completed summary
```

## Error handling
- Cron callback: swallows everything (a raised callback degrades APScheduler); next week retries.
- Sweep job: best-effort. One bad game → counted, not fatal. Infra-unhealthy → skip (job `succeeded`, not `failed`) so a routine cause (cache unmounted) doesn't spam failures.
- Dedup: DB-enforced single in-flight sweep; a manual + cron race collapses to one row.

## Observability
- `sweep.completed` structured summary (the future status-page banner / X5 reads `validation_history` + `games.status`, both already updated per game).
- The sweep job row in `/jobs` (kind='sweep') gives start/finish + success/failure at a glance.

## Testing (TDD)
- `enqueue_validation_sweep`: inserts one sweep row; dedup skips a second; DB error → returns 0, no raise.
- Migration 0005: only one queued/running sweep allowed (second insert conflicts); a finished sweep doesn't block a new one.
- `validate_one_game` parity: same `validation_history` row + status transitions as the old handler (existing validate tests stay green via the thin wrapper).
- Sweep handler: enumerates exactly the `up_to_date`+`validation_failed` steam set (excludes blocked/other statuses, excludes epic); validates each once; a per-game exception is isolated (others still validated, summary counts it); `validator_self_test` False → skip + succeed; summary tallies eviction/recovery correctly; concurrency bounded by `sweep_batch_size`.
- Scheduler: registers the `validation_sweep` cron job when enabled; absent when `validation_sweep_enabled=False`; bad `validation_sweep_cron` → settings fail-fast.

## Scope / deferred
- **In:** weekly cron, steam up_to_date+validation_failed re-validation in batches of 10, validator-health skip, dedup, summary, settings, migration 0005, validate refactor.
- **Deferred (follow-ups):** Epic disk-stat sweep (F7-Epic), manifest-version pruning (keep latest 3), incremental/changed-manifest-only validation (Manifesto §63 — only if the sweep exceeds 30 min), `SWEEP_WARN_HOURS` + the status-page sweep banner (status-page feature), a `sweep_runs` summary table.
