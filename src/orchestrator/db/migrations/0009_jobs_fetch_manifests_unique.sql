-- 0009_jobs_fetch_manifests_unique.sql
-- Add the 'fetch_manifests' job kind (DepotDownloader manifest-only fetch that
-- closes the validation-coverage gap) and enforce at most one in-flight job.
--
-- SQLite cannot ALTER CHECK constraints directly.  Recipe mirrors 0002: snapshot
-- data into a backup table, drop original, recreate with extended CHECK, restore
-- data, drop backup.  After restoring, recreate every index that existed on
-- `jobs` after migration 0008 (base indexes from 0001/0002 plus in-flight indexes
-- from 0004-0007) plus the new fetch_manifests in-flight index.

PRAGMA foreign_keys = OFF;

-- 1. Snapshot existing rows (no constraints).
CREATE TABLE jobs_backup AS SELECT * FROM jobs;

-- 2. Drop all indexes on jobs then drop the table.
DROP INDEX IF EXISTS idx_jobs_state_kind;
DROP INDEX IF EXISTS idx_jobs_started;
DROP INDEX IF EXISTS idx_jobs_finished;
DROP INDEX IF EXISTS idx_jobs_dedupe;
DROP INDEX IF EXISTS idx_jobs_library_sync_inflight;
DROP INDEX IF EXISTS idx_jobs_sweep_inflight;
DROP INDEX IF EXISTS idx_jobs_prefill_inflight;
DROP INDEX IF EXISTS idx_jobs_validate_inflight;
DROP INDEX IF EXISTS idx_jobs_manifest_fetch_inflight;
DROP TABLE jobs;

-- 3. Recreate with the extended kind CHECK (adds 'fetch_manifests').
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN (
                      'prefill','validate','library_sync','auth_refresh',
                      'sweep','manifest_fetch','fetch_manifests')),
    game_id       INTEGER REFERENCES games(id) ON DELETE SET NULL,
    platform      TEXT CHECK (platform IS NULL OR platform IN ('steam','epic')),
    state         TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    progress      REAL CHECK (progress IS NULL OR (progress >= 0.0 AND progress <= 1.0)),
    source        TEXT NOT NULL DEFAULT 'scheduler'
                  CHECK (source IN ('scheduler','cli','gameshelf','api')),
    started_at    TEXT,
    finished_at   TEXT,
    error         TEXT,
    payload       TEXT
) STRICT;

-- 4. Restore rows from the backup.
INSERT INTO jobs (id, kind, game_id, platform, state, progress, source,
                  started_at, finished_at, error, payload)
SELECT id, kind, game_id, platform, state, progress, source,
       started_at, finished_at, error, payload
FROM jobs_backup;

-- 5. Drop backup.
DROP TABLE jobs_backup;

-- 6. Recreate base indexes (0001/0002).
CREATE INDEX idx_jobs_state_kind ON jobs(state, kind);
CREATE INDEX idx_jobs_started ON jobs(started_at DESC) WHERE started_at IS NOT NULL;
CREATE INDEX idx_jobs_finished ON jobs(finished_at) WHERE finished_at IS NOT NULL AND error IS NULL;
CREATE INDEX idx_jobs_dedupe ON jobs(game_id, kind, state)
    WHERE state IN ('queued', 'running');

-- 7. Recreate in-flight unique indexes (0004-0007).
CREATE UNIQUE INDEX idx_jobs_library_sync_inflight
    ON jobs(platform)
    WHERE kind = 'library_sync' AND state IN ('queued', 'running');

CREATE UNIQUE INDEX idx_jobs_sweep_inflight
    ON jobs(kind)
    WHERE kind = 'sweep' AND state IN ('queued', 'running');

CREATE UNIQUE INDEX idx_jobs_prefill_inflight
    ON jobs(game_id)
    WHERE kind = 'prefill' AND state IN ('queued', 'running');

CREATE UNIQUE INDEX idx_jobs_validate_inflight
    ON jobs(game_id)
    WHERE kind = 'validate' AND state IN ('queued', 'running');

CREATE UNIQUE INDEX idx_jobs_manifest_fetch_inflight
    ON jobs(game_id)
    WHERE kind = 'manifest_fetch' AND state IN ('queued', 'running');

-- 8. New fetch_manifests in-flight dedup index.
-- Cancel any pre-existing duplicate in-flight fetch_manifests rows before
-- creating the index, so it applies cleanly to already-deployed databases.
UPDATE jobs
SET state = 'cancelled',
    finished_at = CURRENT_TIMESTAMP,
    error = 'superseded: duplicate in-flight fetch_manifests (migration 0009 dedup)'
WHERE kind = 'fetch_manifests'
  AND state IN ('queued', 'running')
  AND id NOT IN (
      SELECT MIN(id) FROM jobs
      WHERE kind = 'fetch_manifests' AND state IN ('queued', 'running')
  );

CREATE UNIQUE INDEX idx_jobs_fetch_manifests_inflight
    ON jobs(kind)
    WHERE kind = 'fetch_manifests' AND state IN ('queued', 'running');

PRAGMA foreign_keys = ON;
