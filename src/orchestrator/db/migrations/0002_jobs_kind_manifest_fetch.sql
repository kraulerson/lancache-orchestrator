-- 0002_jobs_kind_manifest_fetch.sql
-- BL12: extend the `jobs.kind` CHECK constraint to include 'manifest_fetch'.
--
-- SQLite can't ALTER CHECK constraints directly. Recipe: snapshot data
-- into a backup table, drop the original, recreate with the new
-- constraint, restore data, drop the backup. This produces a regex-clean
-- migration (every CREATE TABLE has a matching DROP TABLE so the
-- migration runner's `_expected_tables_for` tracker ends up with just
-- `jobs` — same as before, but with the extended CHECK).

PRAGMA foreign_keys = OFF;

-- 1. Snapshot existing rows into a temporary table (no constraints).
CREATE TABLE jobs_backup AS SELECT * FROM jobs;

-- 2. Drop indexes + table to clear the old CHECK constraint.
DROP INDEX IF EXISTS idx_jobs_state_kind;
DROP INDEX IF EXISTS idx_jobs_started;
DROP INDEX IF EXISTS idx_jobs_finished;
DROP INDEX IF EXISTS idx_jobs_dedupe;
DROP TABLE jobs;

-- 3. Recreate with the extended kind CHECK (adds 'manifest_fetch').
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('prefill','validate','library_sync','auth_refresh','sweep','manifest_fetch')),
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

-- 6. Recreate indexes to match 0001 definitions exactly.
CREATE INDEX idx_jobs_state_kind ON jobs(state, kind);
CREATE INDEX idx_jobs_started ON jobs(started_at DESC) WHERE started_at IS NOT NULL;
CREATE INDEX idx_jobs_finished ON jobs(finished_at) WHERE finished_at IS NOT NULL AND error IS NULL;
CREATE INDEX idx_jobs_dedupe ON jobs(game_id, kind, state)
    WHERE state IN ('queued', 'running');

PRAGMA foreign_keys = ON;
