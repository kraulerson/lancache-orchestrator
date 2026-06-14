# UAT-11 — Automated Suite Leg

- **Date:** 2026-06-13
- **Branch:** `main` (includes F11 CLI, F13 sweep, full-codebase audit remediation PRs #148/#149, jobs-worker quick-wins PR #150 / commit 8e78bce)
- **HEAD:** `f656119`
- **Persona:** QA Test Engineer
- **Environment:** project `.venv` (Python 3.12), macOS (darwin 25.4.0)
- **Tooling:** pytest 8.4.2, mypy 2.1.0, ruff 0.15.11, semgrep 8.30.1, gitleaks (homebrew)

## Overall verdict: PASS

Every automated gate passes clean. No failures, no type errors, no lint/format drift, zero security findings, license gate green. No hang in the pool concurrency tests (full suite completed in ~30s).

## Gate Results

| # | Gate | Command | Result | Detail |
|---|------|---------|--------|--------|
| 1 | Full test suite | `pytest -p no:randomly -q` | PASS | 1165 passed, 0 failed, 3 deselected — 30.47s |
| 2 | Type checking | `mypy --strict src/` | PASS | Success: no issues found in 78 source files |
| 3a | Lint | `ruff check src/ tests/` | PASS | All checks passed (exit 0) |
| 3b | Format | `ruff format --check src/ tests/` | PASS | 185 files already formatted (exit 0) |
| 4a | SAST | `semgrep --config .semgrep/orchestrator-rules.yaml src/ --error` | PASS | 7 rules / 78 files / 0 findings (exit 0) |
| 4b | Secrets | `gitleaks detect --no-banner` | PASS | 207 commits scanned, no leaks found (exit 0) |
| 5 | License gate | `pytest tests/test_licenses.py -q` | PASS | 1 passed in 0.32s |
| 6 | Coverage sanity | targeted `--cov` on hot modules | PASS | 85–89% per module; gaps are defensive error branches only |

## Failures

None.

## Deselected tests (3)

All three are `slow`-marked sustained-workload tests, intentionally excluded by the default `addopts = ["-m", "not slow"]` in `pyproject.toml`. They are NOT skips/errors:

- `tests/db/test_pool_slow.py::test_sustained_concurrent_workload`
- `tests/db/test_pool_slow.py::test_replacement_storm_guard_under_load`
- `tests/db/test_pool_slow.py::test_long_running_streaming_read_under_concurrent_writes`

These run on demand via `-m slow`. They exist precisely to surface the kind of pool-concurrency regression that could hang the suite. No hang was observed in either the default run or the targeted pool runs.

## Pool concurrency hang check

Explicitly watched per the brief. Full suite finished in **30.47s** (re-run: 31.65s), well under the ~3 min hang threshold. The audit-remediation pool fixes (PR #148: surplus-reader double-close deadlock fix at commit 3f32319, regression test 63312e0) are in place and the dedicated pool suites pass:
`test_pool.py`, `test_pool_concurrency.py`, `test_pool_concurrency_audit.py`, `test_pool_chaos.py`, `test_pool_property.py`, `test_pool_reader_exhaustion.py` — all green.

## Coverage sanity — recently-changed modules

Recently changed src modules (last 6 commits, `git diff --stat HEAD~6 HEAD`) and their test mapping. Every changed module has a corresponding test file; no orphaned modules.

Targeted coverage (running only each module's own test files — full-suite coverage is higher):

| Module | Stmt cov | Branch cov | Owning tests | Assessment |
|--------|----------|-----------|--------------|------------|
| `jobs/worker.py` | 88% | — | `tests/jobs/test_worker.py`, `tests/jobs/test_reaper.py`, `tests/platform/steam/test_worker_audit.py` | Adequate. New correlation-context + max-runtime self-heal code (commit 8e78bce) is covered; `test_worker.py` references runtime/cancel/timeout 8×, and `test_reaper.py` covers the self-heal reaper. |
| `db/pool.py` | 85% | — | 7 pool test files incl. chaos/property/concurrency-audit | Strong — most heavily tested subsystem. |
| `validator/self_test.py` | 89% | — | `tests/validator/test_self_test.py` | Adequate. |
| `db/migrate.py` | 88% | — | `tests/db/test_migrate.py` | Adequate; migration 0006 (prefill/validate dedup) present. |
| `cli/base.py` | — | — | covered indirectly via `test_cmd_*.py`, `test_main.py`, `test_client.py` | No dedicated `test_base.py`, but the error-formatting/command-base surface is exercised across the CLI command suites (config/db/auth/game/jobs/library/status). Minor structural note, not a gap blocking sign-off. |

### Uncovered lines — characterization

The uncovered lines in the hot modules are **defensive error-handling branches**, not core-logic gaps:

- `jobs/worker.py`: retry-exhaustion `raise` in `_write_job_status_with_retry`, `mark_failed_failed` / `mark_succeeded_failed` logging fallbacks, claim-failed continue path.
- `validator/self_test.py` (54–56): the `except OSError` self-test failure fallback.

These are deliberately-hard-to-trigger failure paths (double-failure scenarios). Their presence is good defensive engineering; lack of direct coverage on them is acceptable for this leg.

## Coverage gaps / follow-up candidates (non-blocking)

1. **`cli/base.py` has no dedicated unit test file.** It is covered transitively, but a focused `test_base.py` for error-formatting/exit-code behavior would tighten the F11 CLI safety net. Recommend filing as a low-priority follow-up.
2. **Defensive double-failure branches** in `jobs/worker.py` and `validator/self_test.py` are uncovered (see above). Optional hardening, not required.

## Sign-off

Automated gate: **PASS**. No SEV-1/SEV-2 blockers from the automated leg. Cleared to proceed with the remaining UAT-11 legs (exploratory / manual).
