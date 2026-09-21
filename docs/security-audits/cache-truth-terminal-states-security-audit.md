# Security audit — retire the unreachable 'failed' status (#316) + verify the job-outcome writer (#317)

**Date:** 2026-09-21
**Branch:** `fix/316-317-cache-truth-terminal-states`
**Phase:** 2.4 (Build Loop `cache-truth-terminal-states`, step 4 of 6)
**Persona:** Senior Security Engineer.

**Findings: 0 (SEV-1: 0, SEV-2: 0, SEV-3: 0, SEV-4: 0).**

## Scope

| file | state |
|---|---|
| `src/orchestrator/db/migrations/0018_retire_failed_status.sql` | new |
| `src/orchestrator/db/migrations/CHECKSUMS` | one line appended |
| `tests/db/test_migration_0018_retire_failed_status.py` | new |

Plus one live action: a deliberately-failing prefill (job 46651, game 15495) to
exercise `record_job_outcome()` in production for the first time.

## Automated

```
gitleaks --no-git --source .../0018_retire_failed_status.sql   -> no leaks found
semgrep p/owasp-top-ten + p/security-audit + .semgrep/ --error -> rc=0
pytest tests/test_measurement_writer_guard.py                  -> 2 passed
pytest (full suite)                                            -> 1993 passed, 3 deselected
```

The writer-guard result is the one that matters most here: it is the
build-breaking source scan that enforces the single-writer rule for
`games.status`. It still passes, confirming this change did not introduce a
second Python writer of cache truth.

## The one question worth asking of a migration

**Does writing `games.status` from SQL evade the single-writer guard?**

The guard (`tests/test_measurement_writer_guard.py`) scans `src/orchestrator`
**Python** and permits only `jobs/measurement.py` to assign `games.status` /
`games.status_measured_at`. A `.sql` migration is outside that scan.

This is not an evasion, and the precedent is explicit: migration 0015 — the
migration that *created* the single-writer rule — itself reset 1753 rows of
`games.status` in SQL. The rule governs **application code at runtime**, where
the hazard is a job outcome being silently recorded as a cache finding. A
migration is reviewed, checksum-pinned, applied once inside a single
`BEGIN IMMEDIATE`/`COMMIT`, and cannot run as a side effect of a job.

Three properties keep it honest rather than merely permitted:

1. **`status_measured_at` is not written.** The migration changes the status and
   deliberately leaves the measurement timestamp alone — NULL for all 19 rows.
   Writing it would assert the cache had been inspected, which is the exact
   truth/outcome conflation 0015 exists to prevent. Pinned by
   `test_status_measured_at_stays_null`.
2. **An audit row is written for every change**, carrying `prior`, `new_status`,
   `downward=1` and `commanded=1`. Nothing changes without a trace.
3. **`commanded=1` prevents a self-inflicted denial of service.** 19 downward
   transitions landing in one instant sit inside the circuit breaker's rolling
   window. Uncommanded, against the default threshold of 25, they would consume
   76 % of the budget and could combine with a handful of genuine downward
   measurements to trip the breaker — halting cache-truth writes library-wide
   because of a maintenance migration. `commanded=1` is what #310/0016 added for
   exactly this, and the breaker's count and its partial index both exclude it.

## Blast radius of the data change

19 rows, all already terminal and already excluded from prefill. The predicate is
`status = 'failed'`, verified against live data as matching exactly those 19 and
nothing else. No credential, no path, no user input is involved — the migration
takes no parameters and interpolates nothing.

`last_error` is preserved rather than cleared, so the evidence for why each row
was blocked survives the status change.

**Reversibility.** There is no down-migration (out of scope for MVP, per ADR
0008). Recovery is a one-line `UPDATE games SET status='failed' WHERE id IN (...)`
against the `measurement_transitions` rows this migration writes, which record
the prior value for every affected row. That is a stronger position than 0015
left, which is worth noting because a migration whose own audit trail is the
recovery path is self-documenting.

## The live action (#317)

Job 46651 is a prefill on game 15495 (CHUCHEL, Epic). It is expected to fail at
`handlers/prefill.py:128` with `EpicManifestError: epic manifest API failed:
HTTP 404` — the error already recorded on that row, so the failure is
deterministic and produces **no download traffic**.

Security-relevant properties:

- **No new attack surface.** The endpoint, handler and error path already exist
  and are already reachable; this exercises them rather than adding anything.
- **No credential is exposed.** The bearer token is read from `/root/orch-lxc.env`
  into a shell variable on the LXC and never printed.
- **The expected write is bounded**: `last_job_outcome` and `last_job_outcome_at`
  on one row. If `games.status` or `status_measured_at` also move, that is a
  **SEV-2 finding against migration 0015's design**, not against this change —
  and detecting it is the entire point of running the exercise.

## Operator note

The migration deploy requires a container recreate, which reaps a running sweep
(12 of 15 historical sweep failures were recreate kills). Sweep 46650 was running
when this was written, so the deploy is deliberately held for the inter-sweep
gap. Queuing job 46651 during the sweep is safe — only recreates are not.
