# Security Audit — F13 Scheduled Validation Sweep

**Date:** 2026-06-07
**Scope:** `core/settings.py` (sweep settings + cron validator), `db/migrations/0005_jobs_sweep_unique.sql`, `scheduler/jobs.py` (`enqueue_validation_sweep`), `scheduler/manager.py` (cron job), `jobs/handlers/validate.py` (`validate_one_game` extraction), `jobs/handlers/sweep.py` (sweep handler), `jobs/handlers/__init__.py`, `api/main.py`.
**Persona:** Senior Security Engineer. A 4-lens adversarial workflow (sweep-handler, validate-extraction, migration+enqueue, settings+scheduler) was run over the batch and each finding skeptic-verified.

## Threat review

| Vector | Assessment |
|--------|------------|
| **Untrusted input** | The sweep takes **no external input**. It enumerates `games` rows the orchestrator itself wrote and validates them against the local lancache cache via the existing F7 disk-stat path. The cron callback takes only the pool. No network, no user-supplied data reaches the sweep. |
| **SQL injection** | All sweep SQL is **constant** (`_CANDIDATE_SQL`, the enqueue INSERT) or parameterized (`validate_one_game`'s writes use `?`). No interpolation, no f-string SQL (semgrep `no-f-string-sql` clean). |
| **DoS / resource exhaustion** | Bounded on every axis: weekly cadence (cron), **one in-flight sweep** (DB-enforced `idx_jobs_sweep_inflight`), and **batch-of-10** validation concurrency (`Semaphore(sweep_batch_size)`). Disk-stat I/O runs in a thread executor (F7) so the event loop is not blocked. Per-game errors are isolated and never abort the batch. The candidate set is the operator's own owned, cached library. |
| **Config injection (cron)** | `validation_sweep_cron` is operator-configured; a malformed expression **fails fast** at settings load (`_validate_sweep_cron` constructs `CronTrigger.from_crontab`, raising `ValidationError`) — no late/silent scheduler corruption. |
| **Credential handling** | The sweep touches **no credentials/tokens**. It re-uses `validate_one_game` which reads stored manifests + stats cache files; no auth, no secrets logged. The sweep summary log carries only counts. |
| **Privilege / state** | The sweep can only move a game between `up_to_date` and `validation_failed` (via the existing, audited validate status logic, including the UAT-10 #3 transient-`downloading` rule). It cannot delete data or escalate. A skip on validator-unhealthy/no-steam-client marks the job **succeeded** (no failure storm). |
| **Supply chain** | Migration `0005` is pinned in `CHECKSUMS` (SHA-256), consistent with the tamper-evidence convention. |

## Findings

**0 open security findings.** The 4-lens adversarial review confirmed **2 correctness/stability defects** (no security-severity), both **fixed in-batch test-first** and re-greened:

1. **`evicted` drift metric miscounted `error` outcomes** (SEV-3, correctness) — the sweep counted any non-`cached` outcome as an eviction, so an infra/data `error` (cache hiccup, purged manifests — which leaves `games.status` unchanged) inflated F13's primary operator signal. Fixed: `evicted` now gates on a genuine regression (`outcome in ('partial','missing')`), and the summary surfaces `validation_error` so error games aren't silently absent.
2. **Sweep concurrency outran the strictly-serial steam worker** (SEV-3, stability) — 10 concurrent `manifest_expand` IPC calls queued head-of-line at the single worker while their per-request timeout clocks ran from dispatch, spuriously timing out trailing large-manifest games. Fixed: a single-flight `asyncio.Lock` on `SteamWorkerClient.manifest_expand` serializes the worker-bound call (timeout now starts at worker-availability) and bounds on-disk temp blobs to one; the disk-stat fan-out stays parallel. Uncontended for the existing single-caller paths (F7 validate, F5 prefill).

The review's other lenses verified `validate_one_game`'s byte-for-byte parity, migration 0005's invariant + checksum, the cron/timezone handling, and the enqueue dedup as sound. No new dependency, no new untrusted-input surface, no new SQL interpolation.

## Residual / accepted
- A missed weekly fire during prolonged downtime replays **once** on next start (`coalesce=True`, `misfire_grace_time=None`) — intended (one catch-up sweep, not a burst).
- The sweep job occupies the single jobs worker for its run (minutes, weekly at 03:00) — accepted per design; user-triggered jobs queue behind it briefly.
- Epic disk-stat validation and manifest-version pruning remain **deferred** (spec's deferred list), not regressions.
