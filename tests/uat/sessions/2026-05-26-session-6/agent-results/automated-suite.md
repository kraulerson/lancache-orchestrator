# UAT-6 — Automated Test Suite Agent Findings

Branch: `uat/session-6` @ `d04b492` (same tree as `main` HEAD post BL11 + dependabot #104/#105/#106)
Run date: 2026-05-26
Host venv: `.venv/` (Python 3.12.13)

## Gate results

| Gate | Status | Notes |
|---|---|---|
| pytest tests/ | PASS | 722 passed, 1 expected-fail (`test_licenses.py::test_all_licenses_in_allowlist` — pip-licenses not installed in venv; pre-existing, not a UAT finding), 3 deselected (slow marker), 453 warnings (see below) |
| ruff check src/ tests/ | PASS | "All checks passed!" |
| ruff format --check src/ tests/ | PASS | 90 files already formatted |
| mypy --strict src/ | PASS | "Success: no issues found in 37 source files" (one note about unused `apscheduler.*`/`spikes.*` overrides — long-standing, not new) |
| gitleaks detect | PASS | 155 commits scanned, ~4.9 MB, "no leaks found" |
| semgrep --config=p/owasp-top-ten src/ | PASS | 152 rules / 39 files / **0 findings** (ran via system `/opt/homebrew/bin/semgrep` v1.157.0; `.venv/bin/semgrep` is not present in this venv — pre-existing, not a UAT finding) |

## Test suite summary

- **Total:** 722 passed, 1 failed (expected — pip-licenses missing), 3 deselected, 0 errors.
- **No regressions vs BL10 baseline.** Verified by checking out `31839ab` (BL10 substrate) and re-running with `-W default`: ResourceWarning count was identical (3), so the warnings present at HEAD are not introduced by BL11 or the dep bumps.
- **Warnings under `-W default` (453 total):** dominated by the known `pydantic_settings` `UserWarning: directory "/run/secrets" does not exist` (already filtered to `ignore` via `pyproject.toml [tool.pytest.ini_options].filterwarnings`, but still emitted when filter is bypassed). The remaining noteworthy entries:
  - 3 × `ResourceWarning: <aiosqlite.core.Connection ...> was deleted before being closed.` — **pre-existing**, identical count on BL10 baseline. Worth a separate hygiene ticket but NOT a UAT-6 regression.
  - No `DeprecationWarning`, `PendingDeprecationWarning`, or `FutureWarning` from gevent / zstandard / ruff observed.
- **New warnings introduced by dep bumps:** None observed at runtime. (Note: `gevent` and `zstandard` are only exercised inside the production Steam worker venv at `/opt/orchestrator/venv-steam-worker/bin/python` per `src/orchestrator/core/settings.py:steam_worker_python_path` — they are NOT installed in this dev `.venv`, so the steam worker subprocess is mocked in tests. Real runtime warnings from the bumped libs can only be surfaced in an integration environment with the production worker venv built.)

## Dependency state

| Package | Installed in `.venv` | `requirements-*.txt` | Match? |
|---|---|---|---|
| gevent | not installed | 26.5.0 (`requirements-steam-worker.txt`) | N/A — lives in separate steam-worker venv (`/opt/orchestrator/venv-steam-worker/`) |
| zstandard | not installed | 0.25.0 (`requirements-steam-worker.txt`) | N/A — same as above |
| ruff | **0.15.11** | **0.15.14** (`requirements-dev.txt`) | **NO — dev venv stale by 3 patch versions** |

The dev `.venv` has not been re-synced after PR #105 merged. Pre-commit hooks / CI still pass because ruff 0.15.11 ⊂ 0.15.14 rule set (no new findings emerged), but the local environment drifts from the pinned lockfile.

## Findings (proposed SEV labels)

- **SEV-3 — Dev `.venv` ruff out of sync with `requirements-dev.txt` (0.15.11 vs 0.15.14).** Reproduce: `.venv/bin/pip show ruff | grep Version` returns `0.15.11`, while `grep '^ruff==' requirements-dev.txt` returns `0.15.14`. Impact: developer-local lint may miss new diagnostics that CI catches (or vice versa). Fix: `.venv/bin/pip install -r requirements-dev.txt`. Process gap: no automation alerts the Orchestrator to re-sync after dependabot merges.
- **SEV-3 — gevent/zstandard bumps not verifiable in this environment.** The steam worker production venv (`/opt/orchestrator/venv-steam-worker/`) does not exist on this dev host. Tests cover the IPC contract via mocks, so a runtime regression in gevent 26.x or zstandard 0.25 would NOT be caught by `pytest tests/`. Recommend a follow-up: stand up the worker venv on the lancache host and run a smoke session against a real Steam account before considering UAT-6 closed. (Compatible with the existing F1 plan — milestone 3/3 is the production cutover.)
- **SEV-3 (pre-existing, not regression) — 3 × aiosqlite `ResourceWarning` for un-closed Connection objects.** Identical count on BL10 baseline `31839ab`. Surfacing here for the burndown backlog, not as a BL11 finding.

No new TODO / FIXME / XXX / HACK comments were added in BL11 source files (`git log -p dfcfe7e^..HEAD -- src/` searched for added comment markers — zero matches).

## Sign-off

**PASS** (suite green, all gates clean, no regressions vs BL10 baseline).

Three SEV-3 hygiene items above are surfaced for triage but none block UAT-6 closure on automated-suite grounds. Recommend Orchestrator confirm: (a) re-sync dev `.venv` ruff to 0.15.14, (b) plan a real-worker smoke test before F1 milestone 3/3 to validate the gevent/zstandard bumps in production conditions, (c) consider an aiosqlite-cleanup ticket for the burndown.
