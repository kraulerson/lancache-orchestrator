# UAT 15 — Live systems agent (read-only, all three hosts)

**Verdict: 6 of 7 PASS, 1 FAIL (SEV-2).**

1. **Migration 0015 applied — PASS.** Four columns present, `measurement_transitions` STRICT
   with CHECK + cascade FK, both indexes, DDL byte-identical to the migration file. Checksum
   agrees three ways (schema_migrations row, CHECKSUMS manifest, `git show main:`). IDs 1-15
   contiguous.
2. **Single-writer rule holds in production data — PASS.** 7144 transition rows since deploy,
   zero out-of-vocabulary `new_status`. 9 rows have `status_measured_at` with no transition,
   all migration-seeded. Post-migration writes with no transition: 0.
3. **Circuit breaker — PASS on config, UNEXERCISED in production.** Threshold unset in env,
   effective 25 / 60 min, Kuma push configured. Exactly ONE downward transition ever
   (game 195, up_to_date -> validation_failed, 2026-09-08 21:05:30). Breaker never tripped,
   therefore never proven.
4. **Epic prefill guard — PASS.** Zero prefill jobs of any kind since 2026-09-08. Scheduler
   predicate run live: eligible = 0. **Under the OLD predicate 17 Epic games would be queued
   for download right now**; `status_measured_at IS NOT NULL` blocks all 17.
   Log half UNVERIFIABLE — container recreated at 20:00:29, no on-disk log persistence.
5. **Steam prefill wrapper — PASS.** Both passes capture `rc`, always log a terminal line,
   `push_kuma down` + `exit 1` on failure. The always-exit-0 bug is gone. Last 10 runs
   (20 passes) all ok, no SKIP/TIMEOUT/KUMA-PUSH-FAILED.
6. **Agent container config survived recreate — PASS.** `User=0:0`, uid 0 confirmed by exec,
   Memory = 8 GiB exactly, `Dns=[192.168.1.40]` present, **256/256 cache buckets visible**.
7. **Sweep behaviour — FAIL (SEV-2).** 16 sweeps since 0015: 1 succeeded, 15 failed.
   3 hit the 6h cap; **12 were killed by container recreates (ID6 reaper)**. The single
   success measured 9 games / 0.1 GiB — a near-empty pass.

| job | state | window | dur | measured | GiB | GiB/h |
|---|---|---|---|---|---|---|
| 46541 | failed | 09-12 15:00-20:00 | 5.01h | 1089 | 3569.7 | 712.8 |
| 46535 | failed | 09-12 03:00-09:00 | 6.00h | 160 | 3834.8 | 639.1 |
| 46534 | failed | 09-11 21:00-21:55 | 0.92h | 46 | 385.7 | 418.1 |
| 46528 | failed | 09-11 09:00-15:00 | 6.00h | 1592 | 3953.4 | 658.9 |
| 46522 | failed | 09-10 21:00-03:00 | 6.00h | 211 | 3843.5 | 640.6 |
| 46519 | failed | 09-10 15:00-18:34 | 3.58h | 633 | 782.8 | 218.7 |

Library = 8276.4 GiB across 757 sized games (2455 of 3212 have no manifest, cost 0).
At 640-713 GiB/h a pass needs 11.6-12.9h against `job_max_runtime_sec = 21600` (6h)
(confirmed at `src/orchestrator/core/settings.py:363`). A complete sweep is structurally
impossible. Consequence: `sweep.completed` / `sweep.aborted` cannot be observed, so the
breaker-abort event and the resumable-ordering claim are unproven in production.
Measurement freshness is nonetheless healthy (oldest attempt 2026-09-11 13:02:58, none NULL).

**Also SEV-3:** orchestrator recreated 12+ times in 4 days; each destroys the only copy of
the logs (no on-disk persistence on the LXC). Two of those recreates were made during this
session's config changes.

**SEV-4:** 19 games permanently stuck at `status='failed'` — no code writes that value any
more and no measurement can clear it (every attempt errors `no_manifest_in_cache` and takes
the attempt-only path). 15 are Epic. Correctly excluded from prefill.

**SEV-4:** `record_job_outcome()` has never fired in production; `last_job_outcome` NULL on
all 3212 rows. Half the cache-truth/job-outcome split untested outside unit tests.
