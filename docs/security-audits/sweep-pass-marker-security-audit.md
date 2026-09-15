# Security Audit — Sweep pass marker (#311)

**Feature:** sweep-pass-marker (a pass boundary that spans runs, plus a cooperative deadline so a cut-off sweep is a success rather than a cancelled job)
**Modules:**
- `src/orchestrator/db/migrations/0017_sweep_pass.sql` — one-row `sweep_pass` marker
- `src/orchestrator/jobs/sweep_pass.py` — `read_pass()` / `complete_pass()`
- `src/orchestrator/jobs/handlers/sweep.py` — pass-gated candidate SQL, deadline, `JobSummary`
- `src/orchestrator/core/settings.py` — `sweep_deadline_margin_sec` + its boot guard
**Audit date:** 2026-09-14
**Auditor:** self-review (Senior Security Engineer persona) + semgrep `--config=auto` + gitleaks + ruff (flake8-bandit `S`) + mypy + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-09-14 -->

## Scope

A full validation pass costs ~12.5 h (8.1 TiB at ~650 GiB/h) against a 6 h `job_max_runtime_sec`, so `sweep.completed` could never fire and the Uptime Kuma sweep monitor could never go green; 15 of 16 sweeps since migration 0015 were recorded `failed`. This adds the missing pass boundary — candidates are gated on `pass_started_at`, so an empty candidate set is a proof of coverage — and replaces the hard `asyncio.wait_for` cancellation with a cooperative deadline that returns honest partial progress.

No new dependency, no new endpoint, no new external input. One migration.

## Methodology

1. **SAST.** `semgrep --config=auto --error` over the three changed source files — clean. `ruff check src tests` (flake8-bandit `S`) — clean.
2. **Secrets.** `gitleaks detect` over 460 commits — no leaks found.
3. **Type safety.** `mypy src` — clean, 110 files.
4. **Single-writer guard.** `tests/test_measurement_writer_guard.py` green: no new code writes `games.status` or `status_measured_at`.
5. **Threat-model cross-check:** injection, availability/monitoring integrity, data destruction, privilege.
6. **Tests.** Full suite 1903 passed, 3 deselected. 19 new tests.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| 1 | SEV-3 | A `sweep_deadline_margin_sec` at or above `job_max_runtime_sec` makes every sweep a silent no-op that still reports healthy | **Fixed in this change** |

### Finding 1 — a margin that swallows the budget reports green while measuring nothing

**Found by:** self-review of the new setting, not by a scanner.

The handler stops starting games once `job_max_runtime_sec - sweep_deadline_margin_sec` has elapsed. With a margin >= the budget that instant is already past when the sweep starts, so the sweep attempts nothing, completes no pass, and returns `JobSummary(ok=True, "pass N partial: 0/3212 games")` — pushing the monitor **green** forever while cache truth silently ages out. That is strictly worse than the DOWN state this feature exists to fix: a monitoring-integrity failure that presents as health.

**Fix:** a `model_validator(mode="after")` on `Settings` rejects the combination at boot with a message naming both values. `job_max_runtime_sec = 0` disables the worker budget entirely, so there is no deadline and the margin is unconstrained. Two tests cover both branches. Fail-loud at boot, per the project's no-silent-fallback standard.

## Non-findings (explicitly checked, clean)

- **No injection.** The one new bound parameter is `pass_started_at`, read from the `sweep_pass` row the migration seeded and written only by `CURRENT_TIMESTAMP`; it never leaves the database and is passed as a `?` placeholder, never interpolated. No request data reaches the sweep's SQL — the sweep has no caller-supplied input beyond the existing `{"full": true}` boolean, whose JSON parse already falls back safely.
- **No new external surface.** No endpoint, no client call, no file path, no subprocess. The marker is internal state written by one job handler.
- **No data destruction.** `complete_pass()` performs a single guarded `UPDATE` of one row's counter and timestamp. Nothing deletes, and cache truth is untouched: the new code calls no measurement writer, so the migration-0015 single-writer rule is preserved and enforced by the build-breaking guard test.
- **Stale-view double-advance.** `complete_pass()` pins its `UPDATE` to `pass_number = <the value read>`, so a caller working from a stale view cannot advance a pass twice. This matters beyond a skipped counter: a second advance would restamp `pass_started_at` and silently excuse every game measured in between from the new pass — a coverage gap that would present as a clean pass. Covered by `test_complete_pass_is_idempotent_against_a_stale_view`.
- **Availability improved, not reduced.** The deadline removes a mid-flight cancellation: the sweep now stops *between* games, so no validate is interrupted part-way and no attempt-write lands after the job row is already terminal (#314). The margin default of 1800 s is set to exceed the longest known single validate (ARK ModKit, 244 GB / 359,671 chunks, ~22 min), and the reasoning is recorded next to the setting so a larger title prompts a review rather than a silent overrun.
- **Breaker semantics unchanged.** A tripped `CircuitBreakerTripped` still aborts, still re-raises, still fails the job, and now additionally does **not** advance the pass — an aborted run cannot be mistaken for coverage. Covered by a test.
- **Monitoring semantics deliberately widened, and bounded.** Sweeps no longer report `failed` for hitting the cap, which is the point: `failed` on a sweep once again means genuinely broken. The remaining ways a sweep pushes DOWN are a tripped breaker or an unhandled exception — both real faults. Finding 1 above is the one way this widening could have gone wrong, and it is now rejected at boot.
- **`full` mode cannot forge coverage.** The Game_shelf full-sweep button is not pass-gated, so it neither filters on the marker nor advances it. A manual run therefore cannot stand in as proof of a completed pass — the same reasoning as `monitor_url_for()` refusing to heartbeat a non-scheduler run.

## Decision

**Cleared to advance.** One SEV-3 found and fixed within the change; no SEV-1/2 findings. Injection-free, secret-free, additive schema, no new dependency or attack surface, and a net reduction in mid-flight cancellation.

## Sign-off

- Implementation: commit `<pending>`
- Test suite: 1903 passed, 3 deselected; 19 new tests (migration 4, pass marker + handler 13, settings guard 2)
- semgrep, gitleaks, ruff, mypy all clean
- Migration 0017 (additive `CREATE TABLE` + one seeded row); no backfill of existing tables
- Live path unexercised in production until deploy — see the deploy note in CHANGELOG
