-- 0005_jobs_sweep_unique.sql
-- F13: at most one queued/running validation `sweep` job at a time. Mirrors the
-- library_sync inflight guard (0004). The cron enqueue uses
-- INSERT ... ON CONFLICT DO NOTHING; the worker's queued -> running ->
-- succeeded/failed transitions keep at most one row in this partial index.

-- Cancel any pre-existing duplicate in-flight sweeps before creating the index,
-- so it applies cleanly to already-deployed databases. Keep the earliest.
UPDATE jobs
SET state = 'cancelled',
    finished_at = CURRENT_TIMESTAMP,
    error = 'superseded: duplicate in-flight sweep (migration 0005 dedup)'
WHERE kind = 'sweep'
  AND state IN ('queued', 'running')
  AND id NOT IN (
      SELECT MIN(id) FROM jobs
      WHERE kind = 'sweep' AND state IN ('queued', 'running')
  );

CREATE UNIQUE INDEX idx_jobs_sweep_inflight
    ON jobs(kind)
    WHERE kind = 'sweep' AND state IN ('queued', 'running');
