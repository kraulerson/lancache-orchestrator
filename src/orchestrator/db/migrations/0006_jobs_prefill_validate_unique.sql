-- 0006_jobs_prefill_validate_unique.sql
-- Audit 2026-06-09 (SEV-3 / SEV-4): prefill and validate jobs had no in-flight
-- UNIQUE constraint (unlike library_sync/sweep, migrations 0004/0005), so the
-- app-level SELECT-then-INSERT dedup in routers/prefill_trigger.py,
-- routers/validate_trigger.py, and jobs/handlers/prefill.py races onto duplicate
-- in-flight rows on concurrent triggers (operator double-click, CLI racing the
-- API, or a duplicate prefill enqueuing a duplicate validate). Enforce the
-- invariant in the database: at most one queued/running prefill — and at most
-- one queued/running validate — per game. The call sites use
-- INSERT ... ON CONFLICT DO NOTHING; the worker's queued -> running ->
-- succeeded/failed transitions keep at most one row per game in each index.

-- Cancel any pre-existing duplicate in-flight prefill rows before creating the
-- index, so it applies cleanly to already-deployed databases. Keep the earliest
-- (lowest id) per game — typically the row the worker is already acting on.
UPDATE jobs
SET state = 'cancelled',
    finished_at = CURRENT_TIMESTAMP,
    error = 'superseded: duplicate in-flight prefill (migration 0006 dedup)'
WHERE kind = 'prefill'
  AND state IN ('queued', 'running')
  AND id NOT IN (
      SELECT MIN(id) FROM jobs
      WHERE kind = 'prefill' AND state IN ('queued', 'running')
      GROUP BY game_id
  );

CREATE UNIQUE INDEX idx_jobs_prefill_inflight
    ON jobs(game_id)
    WHERE kind = 'prefill' AND state IN ('queued', 'running');

-- Same for validate.
UPDATE jobs
SET state = 'cancelled',
    finished_at = CURRENT_TIMESTAMP,
    error = 'superseded: duplicate in-flight validate (migration 0006 dedup)'
WHERE kind = 'validate'
  AND state IN ('queued', 'running')
  AND id NOT IN (
      SELECT MIN(id) FROM jobs
      WHERE kind = 'validate' AND state IN ('queued', 'running')
      GROUP BY game_id
  );

CREATE UNIQUE INDEX idx_jobs_validate_inflight
    ON jobs(game_id)
    WHERE kind = 'validate' AND state IN ('queued', 'running');
