# UAT Session 17 — Automated Suite

**Date:** 2026-09-21
**Scope:** PR #347 (keys_zone alarm) + PR #353 / migration 0018 (retire `failed` status)
**Branch/commit:** `main` @ `5ee6cfd` (merge of PR #353)

## Verdict: PASS

Every command below ran clean: exit code 0, zero findings, zero regressions. Nothing to triage.

## Summary table

| Check | Result | Exit code |
|---|---|---|
| Full pytest suite | 1993 passed, 3 deselected | 0 |
| Writer guard (`tests/test_measurement_writer_guard.py -v`) | 2 passed | 0 |
| `ruff check src/ tests/` | All checks passed | 0 |
| `ruff format --check src/ tests/` | 295 files already formatted | 0 |
| `mypy --strict src/` | Success: no issues found in 111 source files | 0 |
| `semgrep scan` (owasp-top-ten + security-audit + `.semgrep/`) | 0 findings, 209 rules, 276 files | 0 |
| `gitleaks detect --no-git` | no leaks found | 0 |
| `pip-audit requirements.txt` | No known vulnerabilities found | 0 |
| `pip-audit requirements-dev.txt` | No known vulnerabilities found | 0 |
| Migration checksums (18 files vs `CHECKSUMS`) | 18/18 match | n/a (verified via `shasum -a 256`) |
| Migration id contiguity | 0001..0018, no gaps, no dupes | n/a |

## Raw output

### `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest`

```
PYTEST_EXIT=0
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========== 1993 passed, 3 deselected, 1 warning in 66.94s (0:01:06) ===========
```

Full tail of the run (relevant new-work lines):

```
tests/db/test_migration_0018_retire_failed_status.py ........            [ 63%]
...
tests/test_measurement_writer_guard.py ..                                [ 94%]
tests/tools/test_delete_actor.py ...........                             [ 94%]
tests/tools/test_key_budget.py ................................          [ 96%]
tests/tools/test_kuma.py .........                                       [ 96%]
```

Only warning: `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead` — pre-existing, unrelated to this session's changes, not a test failure.

**Baseline comparison:** `CLAUDE.md`'s last recorded figure was 1867 passed / 3 deselected (pre-#347/#353). Current run is 1993 passed / 3 deselected — a net increase of 126 passing tests, consistent with the new `test_kuma.py`, `test_key_budget.py`, and `test_migration_0018_retire_failed_status.py` suites added by these two PRs. The 3 deselected count is unchanged and is the pre-existing `slow`-marked tests (`pyproject.toml` marker `slow: marks tests that simulate sustained workload`), confirmed via:

```
$ .venv/bin/python -m pytest --collect-only -q
1993/1996 tests collected (3 deselected) in 0.67s
```

No regressions.

### `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_measurement_writer_guard.py -v`

This is the build-breaking source scan enforcing that only `jobs/measurement.py::record_measurement()` writes `games.status` / `games.status_measured_at`. **Result: PASS, both assertions held — no other module violates the single-writer rule after PR #347/#353's changes to `tools/cache_catcher/` and the migration 0018 status-value rewrite.**

```
WRITER_GUARD_EXIT=0
collecting ... collected 2 items

tests/test_measurement_writer_guard.py::test_only_measurement_module_writes_cache_truth PASSED [ 50%]
tests/test_measurement_writer_guard.py::test_pattern_catches_the_forms_it_must_and_spares_the_ones_it_must_not PASSED [100%]

============================== 2 passed in 0.12s ===============================
```

### `.venv/bin/ruff check src/ tests/`

```
RUFF_CHECK_EXIT=0
All checks passed!
```

### `.venv/bin/ruff format --check src/ tests/`

```
RUFF_FORMAT_EXIT=0
295 files already formatted
```

### `.venv/bin/mypy --strict src/`

```
MYPY_EXIT=0
pyproject.toml: note: unused section(s): module = ['spikes.*']
Success: no issues found in 111 source files
```

The "unused section" note is a pre-existing config note, not an error — mypy still exits 0.

### `semgrep scan --config=p/owasp-top-ten --config=p/security-audit --config=.semgrep/ --quiet --no-git-ignore --error src/ tests/ tools/`

```
SEMGREP_EXIT=0
```

(No stdout under `--quiet` since there were zero findings — this is expected quiet-mode behavior, not a silent failure.) Re-ran the identical scope without `--quiet` to confirm the ruleset actually loaded and executed, rather than silently no-op'ing:

```
$ semgrep scan --config=p/owasp-top-ten --config=p/security-audit --config=.semgrep/ --no-git-ignore --error src/ tests/ tools/
✅ Scan completed successfully.
 • Findings: 0 (0 blocking)
 • Rules run: 209
 • Targets scanned: 276
 • Parsed lines: ~100.0%
 • Scan skipped:
   ◦ Files matching .semgrepignore patterns: 25
Ran 209 rules on 276 files: 0 findings.
EXIT_NONQUIET=0
```

209 rules (owasp-top-ten + security-audit + the project's own `.semgrep/orchestrator-rules.yaml`) ran across 276 target files under `src/`, `tests/`, `tools/`, including the new `tools/cache_catcher/kuma.py`, `key_budget.py`, `key_budget_probe.py` and the modified `fanotify_guard.py`. Zero findings.

### `gitleaks detect --no-git --source .`

```
GITLEAKS_EXIT=0
4:12PM INF scanned ~41311738 bytes (41.31 MB) in 1.83s
4:12PM INF no leaks found
```

### `.venv/bin/pip-audit --requirement requirements.txt --disable-pip --strict`

```
PIPAUDIT_REQ_EXIT=0
No known vulnerabilities found
```

### `.venv/bin/pip-audit --requirement requirements-dev.txt --disable-pip --strict`

```
PIPAUDIT_DEV_EXIT=0
No known vulnerabilities found
```

### Migration checksum verification (manual `shasum -a 256` against `src/orchestrator/db/migrations/CHECKSUMS`)

18 `.sql` files present, all 18 have a `CHECKSUMS` line, all 18 checksums matched on recompute:

```
OK   0001_initial.sql
OK   0002_jobs_kind_manifest_fetch.sql
OK   0003_manifests_depot_id.sql
OK   0004_jobs_library_sync_unique.sql
OK   0005_jobs_sweep_unique.sql
OK   0006_jobs_prefill_validate_unique.sql
OK   0007_jobs_manifest_fetch_unique.sql
OK   0008_steam_app_info.sql
OK   0009_jobs_fetch_manifests_unique.sql
OK   0010_manifests_cdn_base.sql
OK   0011_prefill_exclusions.sql
OK   0012_prefill_exclusions_gameshelf_source.sql
OK   0013_steam_app_info_categories.sql
OK   0014_jobs_kind_purge.sql
OK   0015_games_measurement_split.sql
OK   0016_commanded_transitions.sql
OK   0017_sweep_pass.sql
OK   0018_retire_failed_status.sql
```

`diff` between the sorted list of ids embedded in filenames and the sorted list of ids in `CHECKSUMS` (excluding comment/blank lines) was empty — no orphaned filenames, no orphaned checksum entries.

### Migration id contiguity

```
$ ls src/orchestrator/db/migrations/*.sql | sed 's/_.*//' | sort
0001
0002
...
0018
```

Diffed against an expected `seq -f "%04g" 1 18` sequence: **identical**. Ids are contiguous 0001 through 0018, no gaps, no duplicates.

## Findings

None. All checks pass with zero findings, zero regressions against the last recorded baseline in `CLAUDE.md` (1867 passing → 1993 passing, growth fully explained by the two shipped PRs' new test files), and full migration-integrity verification (checksums + contiguity) is clean.

Not run / not in scope for this session: live deployment verification of the keys_zone alarm gauge cycle (`CLAUDE.md` already flags the 24h gauge cycle as "still not observed on a real slot" — that's an operational/live-system check, not something this automated suite can exercise) and no manual/exploratory UAT scenarios (this is the automated-suite agent only; other agents in this session presumably cover that).
