-- 0001_initial.sql
-- lancache_orchestrator initial schema
-- Phase 1 Step 1.4 — finalized 2026-04-20
-- References: Brief §5, Data Contract §6, DQ2/DQ3/DQ6/DQ8
--
-- Applied inside BEGIN IMMEDIATE / COMMIT by the migrate runner; on failure
-- the transaction rolls back and schema_migrations stays empty. PRAGMAs that
-- cannot run inside a transaction (journal_mode=WAL, temp_store) are set by
-- the runner before opening the transaction.

-- ----------------------------------------------------------------------------
-- platforms  —  effectively an enum; 2 rows ever: 'steam', 'epic'.
-- FK from games.platform uses ON DELETE RESTRICT (DQ8).
-- ----------------------------------------------------------------------------
CREATE TABLE platforms (
    name              TEXT PRIMARY KEY CHECK (name IN ('steam', 'epic')),
    auth_status       TEXT NOT NULL CHECK (auth_status IN ('ok', 'expired', 'error', 'never')),
    auth_method       TEXT NOT NULL CHECK (auth_method IN ('steam_cm', 'epic_oauth')),
    auth_expires_at   TEXT,
    last_sync_at      TEXT,
    last_error        TEXT,
    config            TEXT                              -- JSON, application-validated
) STRICT;

-- Seed the enum values so later inserts never hit a missing platform.
INSERT INTO platforms (name, auth_status, auth_method) VALUES
    ('steam', 'never', 'steam_cm'),
    ('epic',  'never', 'epic_oauth');

-- ----------------------------------------------------------------------------
-- games  —  owned-library state per (platform, app_id).
-- ----------------------------------------------------------------------------
CREATE TABLE games (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    platform              TEXT NOT NULL REFERENCES platforms(name) ON DELETE RESTRICT,
    app_id                TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 64),
    title                 TEXT NOT NULL,
    owned                 INTEGER NOT NULL DEFAULT 1 CHECK (owned IN (0, 1)),
    size_bytes            INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    current_version       TEXT,                         -- steam: manifest_gid; epic: build_version
    cached_version        TEXT,                         -- last successfully prefilled version
    status                TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN (
                              'unknown','not_downloaded','up_to_date','pending_update',
                              'downloading','validation_failed','blocked','failed')),
    last_validated_at     TEXT,
    last_prefilled_at     TEXT,
    last_error            TEXT,
    metadata              TEXT,                         -- JSON: depots, build hints
    UNIQUE(platform, app_id)
) STRICT;

-- Fast filter by status (e.g., "all games needing prefill").
CREATE INDEX idx_games_status ON games(status);

-- Covering lookup for library-sync upserts — (platform, app_id) already unique.
CREATE INDEX idx_games_platform_app ON games(platform, app_id);

-- Fast "recently prefilled" queries surfaced in status page + Game_shelf Cache dashboard.
CREATE INDEX idx_games_last_prefilled ON games(last_prefilled_at DESC)
    WHERE last_prefilled_at IS NOT NULL;

-- ----------------------------------------------------------------------------
-- manifests  —  parsed manifest per (game, version). raw is compressed BLOB (DQ3).
-- Retention: keep latest 3 versions per game (prune weekly during F13 sweep).
-- ----------------------------------------------------------------------------
CREATE TABLE manifests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id           INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    version           TEXT NOT NULL,
    fetched_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    chunk_count       INTEGER NOT NULL CHECK (chunk_count >= 0),
    total_bytes       INTEGER NOT NULL CHECK (total_bytes >= 0),
    raw               BLOB NOT NULL,                   -- zstd-compressed parsed manifest
    UNIQUE(game_id, version)
) STRICT;

-- "Latest manifest for this game" lookup used every prefill cycle.
CREATE INDEX idx_manifests_game_fetched ON manifests(game_id, fetched_at DESC);

-- ----------------------------------------------------------------------------
-- block_list  —  skip these games during scheduled prefill.
-- NOT a FK to games — allows pre-blocking an app_id the orchestrator hasn't
-- seen yet (per F8 acceptance).
-- ----------------------------------------------------------------------------
CREATE TABLE block_list (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL CHECK (platform IN ('steam', 'epic')),
    app_id      TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 64),
    reason      TEXT CHECK (reason IS NULL OR length(reason) <= 500),
    source      TEXT NOT NULL DEFAULT 'cli'
                CHECK (source IN ('cli','gameshelf','api','config')),
    blocked_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, app_id)
) STRICT;

-- ----------------------------------------------------------------------------
-- validation_history  —  audit trail for every F7 validation run.
-- Retention: 90 days (prune daily).
-- ----------------------------------------------------------------------------
CREATE TABLE validation_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id            INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    manifest_version   TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    method             TEXT NOT NULL CHECK (method IN ('disk_stat','head_probe','mixed')),
    chunks_total       INTEGER NOT NULL CHECK (chunks_total >= 0),
    chunks_cached      INTEGER NOT NULL CHECK (chunks_cached >= 0),
    chunks_missing     INTEGER NOT NULL CHECK (chunks_missing >= 0),
    outcome            TEXT NOT NULL CHECK (outcome IN ('cached','partial','missing','error')),
    error              TEXT
) STRICT;

-- Game-detail page lists most-recent validations for that game.
CREATE INDEX idx_vh_game ON validation_history(game_id, started_at DESC);

-- Pruning scans by started_at ascending.
CREATE INDEX idx_vh_started ON validation_history(started_at);

-- ----------------------------------------------------------------------------
-- jobs  —  every enqueued operation (prefill, validate, library_sync, auth_refresh).
-- ON DELETE SET NULL on game_id: when a game is deleted (rare — only if ownership
-- revoked AND user explicitly prunes), keep the job history for diagnosis.
-- Retention: 90 days for succeeded/failed; indefinite for rows with non-null error.
-- ----------------------------------------------------------------------------
CREATE TABLE jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('prefill','validate','library_sync','auth_refresh','sweep')),
    game_id       INTEGER REFERENCES games(id) ON DELETE SET NULL,
    platform      TEXT CHECK (platform IS NULL OR platform IN ('steam','epic')),
    state         TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    progress      REAL CHECK (progress IS NULL OR (progress >= 0.0 AND progress <= 1.0)),
    source        TEXT NOT NULL DEFAULT 'scheduler'
                  CHECK (source IN ('scheduler','cli','gameshelf','api')),
    started_at    TEXT,
    finished_at   TEXT,
    error         TEXT,
    payload       TEXT                                  -- JSON; NEVER contains credentials
) STRICT;

-- Jobs feed and "active jobs" indicator use (state, kind) predicates.
CREATE INDEX idx_jobs_state_kind ON jobs(state, kind);

-- Recent-jobs listing on status page sorts by started_at DESC.
CREATE INDEX idx_jobs_started ON jobs(started_at DESC) WHERE started_at IS NOT NULL;

-- Pruning scans completed jobs by finished_at.
CREATE INDEX idx_jobs_finished ON jobs(finished_at) WHERE finished_at IS NOT NULL AND error IS NULL;

-- Concurrent-job dedupe (409 on POST if already running) needs a fast lookup.
CREATE INDEX idx_jobs_dedupe ON jobs(game_id, kind, state)
    WHERE state IN ('queued', 'running');

-- ----------------------------------------------------------------------------
-- cache_observations  —  access-log-tail output (DQ2: schema ships in 0001
-- even though MVP does not populate).
-- ----------------------------------------------------------------------------
CREATE TABLE cache_observations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at        TEXT NOT NULL,
    event              TEXT NOT NULL CHECK (event IN ('hit','miss','expired','revalidated','eviction')),
    cache_identifier   TEXT NOT NULL,                   -- 'steam' or '$http_host'
    path               TEXT NOT NULL,
    bytes              INTEGER CHECK (bytes IS NULL OR bytes >= 0)
) STRICT;

CREATE INDEX idx_co_time ON cache_observations(observed_at DESC);
CREATE INDEX idx_co_event_time ON cache_observations(event, observed_at DESC);
