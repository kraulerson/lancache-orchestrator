-- 0014_jobs_kind_purge.sql
-- Add the 'purge' job kind (F18 operator-driven cache purge — delete a game's
-- cached chunk files, then let validate/re-prefill heal it) and a purge in-flight
-- dedup index so at most one queued/running purge per game exists (the API's
-- ON CONFLICT DO NOTHING then dedups concurrent purge triggers).
--
-- SQLite cannot ALTER a CHECK constraint directly.  Recipe mirrors 0009: snapshot
-- data into a backup table, drop the original, recreate with the extended CHECK,
-- restore data, drop the backup.  After restoring, recreate every index that
-- existed on `jobs` after migration 0009 (jobs is untouched by 0010-0013) plus the
-- new purge in-flight index.

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
DROP INDEX IF EXISTS idx_jobs_fetch_manifests_inflight;
DROP TABLE jobs;

-- 3. Recreate with the extended kind CHECK (adds 'purge').
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN (
                      'prefill','validate','library_sync','auth_refresh',
                      'sweep','manifest_fetch','fetch_manifests','purge')),
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

-- 7. Recreate in-flight unique indexes (0004-0009).
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

CREATE UNIQUE INDEX idx_jobs_fetch_manifests_inflight
    ON jobs(kind)
    WHERE kind = 'fetch_manifests' AND state IN ('queued', 'running');

-- 8. New purge in-flight dedup index (mirrors prefill/validate): at most one
--    queued/running purge per game, so the API's ON CONFLICT DO NOTHING dedups
--    concurrent purge triggers.
CREATE UNIQUE INDEX idx_jobs_purge_inflight
    ON jobs(game_id)
    WHERE kind = 'purge' AND state IN ('queued', 'running');

PRAGMA foreign_keys = ON;
