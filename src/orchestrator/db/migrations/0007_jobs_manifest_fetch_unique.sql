-- 0007_jobs_manifest_fetch_unique.sql
-- UAT-11 F-INT-5 (SEV-4): manifest_fetch was the only job kind without a
-- DB-enforced in-flight UNIQUE constraint — its trigger used the same race-prone
-- app-level SELECT-then-INSERT as prefill/validate did before migration 0006.
-- Enforce the invariant in the database: at most one queued/running
-- manifest_fetch per game. The call site uses INSERT ... ON CONFLICT DO NOTHING;
-- the worker's queued -> running -> succeeded/failed transitions keep at most one
-- row per game in this partial index. Mirrors 0004/0005/0006.

-- Cancel any pre-existing duplicate in-flight rows before creating the index, so
-- it applies cleanly to already-deployed databases. Keep the earliest per game.
UPDATE jobs
SET state = 'cancelled',
    finished_at = CURRENT_TIMESTAMP,
    error = 'superseded: duplicate in-flight manifest_fetch (migration 0007 dedup)'
WHERE kind = 'manifest_fetch'
  AND state IN ('queued', 'running')
  AND id NOT IN (
      SELECT MIN(id) FROM jobs
      WHERE kind = 'manifest_fetch' AND state IN ('queued', 'running')
      GROUP BY game_id
  );

CREATE UNIQUE INDEX idx_jobs_manifest_fetch_inflight
    ON jobs(game_id)
    WHERE kind = 'manifest_fetch' AND state IN ('queued', 'running');
