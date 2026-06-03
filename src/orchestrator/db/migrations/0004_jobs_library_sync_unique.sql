-- 0004_jobs_library_sync_unique.sql
-- SEV-3 (code review 2026-06-02): the app-level SELECT-then-INSERT dedup in
-- scheduler/jobs.py and routers/sync.py straddles an await and races onto
-- duplicate in-flight library_sync rows on concurrent cron + API triggers
-- (the existing idx_jobs_dedupe is NON-unique). Enforce the invariant in the
-- database: at most one queued/running library_sync per platform. The call
-- sites use INSERT ... ON CONFLICT DO NOTHING; the worker's normal state
-- transitions (queued -> running -> succeeded/failed) keep at most one row in
-- this partial index at a time.

-- Resolve any pre-existing duplicate in-flight rows (the bug this index
-- prevents) BEFORE creating the UNIQUE index, so the migration applies cleanly
-- to already-deployed databases. Keep the earliest (lowest id) per platform —
-- typically the row the worker is already acting on — and cancel the rest.
UPDATE jobs
SET state = 'cancelled',
    finished_at = CURRENT_TIMESTAMP,
    error = 'superseded: duplicate in-flight library_sync (migration 0004 dedup)'
WHERE kind = 'library_sync'
  AND state IN ('queued', 'running')
  AND id NOT IN (
      SELECT MIN(id) FROM jobs
      WHERE kind = 'library_sync' AND state IN ('queued', 'running')
      GROUP BY platform
  );

CREATE UNIQUE INDEX idx_jobs_library_sync_inflight
    ON jobs(platform)
    WHERE kind = 'library_sync' AND state IN ('queued', 'running');
