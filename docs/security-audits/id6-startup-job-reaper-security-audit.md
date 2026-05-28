# Security Audit — ID6 Startup Job Reaper

**Feature:** ID6-startup-job-reaper
**Audit date:** 2026-05-27
**Audited modules:**
- src/orchestrator/jobs/reaper.py
- src/orchestrator/api/main.py (lifespan integration)

<!-- Last Updated: 2026-05-27 -->

## Scope

Post-implementation security review of the startup orphan-job reaper.

## Methodology

1. ruff / ruff format / mypy --strict — all clean (39 source files)
2. gitleaks full-repo scan — no leaks
3. semgrep p/owasp-top-ten on `src/orchestrator/jobs/reaper.py` — 0 findings
4. Manual review against threat-model entries TM-005 (SQL injection),
   TM-014 (boot-time resource pressure)

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

No new vulnerabilities introduced.

## Threat-model walk

- **TM-005 (SQL injection):** MITIGATED. Single SQL statement,
  parameterized — `?` placeholder for the error message string,
  hard-coded constants for state values. No user input reaches the
  query. The `REAPER_ERROR_MESSAGE` constant is a module-level string
  literal, not an argument.

- **TM-014 (boot-time resource pressure):** MITIGATED. Single
  atomic `UPDATE` (no loop, no per-row processing). For N orphan
  rows, SQLite touches at most N rows in one statement; even
  pathological "orphan-storm" cases (thousands of rows) execute in
  milliseconds. The lifespan wraps the call in `try/except Exception`
  so a database hiccup doesn't abort boot — failed reap logs at
  ERROR; boot continues. The jobs worker that spawns next won't
  claim `running` rows anyway (its SELECT filters `state='queued'`),
  so a missed reap is recoverable on the next restart.

## Defensive-programming review

- Lifespan ordering: reaper runs BEFORE `jobs_worker_task` is created.
  Even if the reaper takes longer than expected, the new worker
  cannot race into the orphan rows because it hasn't been spawned yet.
- The reaper does NOT modify `started_at` on the reaped rows —
  diagnostic data ("how long was this job running before crash?")
  is preserved.
- The reaper does NOT touch `payload` — any IPC context that future
  forensics need is retained.

## Operator surfaces

- `jobs.reaper.reaped_orphans` (WARN) — emitted when count > 0.
  Surfaces that the previous orchestrator process did not shut down
  cleanly.
- `jobs.reaper.no_orphans` (INFO) — emitted when nothing to reap.
  Normal boot path.
- `api.boot.reaped_orphan_jobs` (WARN) — lifespan-level wrapper log
  with the same count.
- `api.boot.reaper_failed` (ERROR) — defensive catch path; never
  expected in normal operation.

No PII, no credentials, no job-payload contents in any log event.

## Sign-off

No SEV findings. ID6 cleared to ship.

— Senior Security Engineer persona, 2026-05-27
