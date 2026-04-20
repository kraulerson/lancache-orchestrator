# Data Model — lancache_orchestrator

**Phase:** 1
**Step:** 1.4
**Generated from:** Brief §5 + `docs/phase-0/data-contract.md` §6–9 + DQ2/DQ3/DQ6/DQ8 resolutions + threat-model §4.3 (storage bottleneck triggers)
**Date:** 2026-04-20
**Status:** Draft — pending Orchestrator review

**Companion artifacts (Phase 2 implementation targets, not yet on disk):**
- `migrations/0001_initial.sql` — canonical SQL in §7 below
- `migrations/0001_initial_down.sql` — rollback in §8 below
- `migrations/_meta_schema_migrations.sql` — migration-runner meta table in §6

**Not applicable:** Step 1.4.5 (Data Migration Plan). The orchestrator replaces SteamPrefill/EpicPrefill in terms of function, but there is **no legacy data to import**. SteamPrefill's flat-file tracker is the source of the problem this project solves; its data is not authoritative and is not being migrated. The orchestrator's first sync cycle derives ground truth from platform APIs + disk-stat, not from any prior tool. Recorded as N/A — no legacy data — in the Project Bible §6.

---

## 1. Schema Overview

Seven entity tables + one meta table. All access via `aiosqlite` with raw parameterized SQL. No ORM, no query builder. Numbered `.sql` migration files applied atomically at container startup by a ~50-LoC runner.

```
┌─────────────────────────────────────────────────────────────────┐
│  platforms  (2 rows ever: 'steam', 'epic')                      │
│    ← RESTRICT FK from games.platform                             │
└─────────────────────────────────────────────────────────────────┘
         ▲
         │ games.platform REFERENCES platforms(name) ON DELETE RESTRICT
         │
┌─────────────────────────────────────────────────────────────────┐
│  games  (~2,600 rows)                                            │
│    1:N → manifests  (ON DELETE CASCADE)                          │
│    1:N → validation_history  (ON DELETE CASCADE)                 │
│    1:N → jobs  (ON DELETE SET NULL — keep history on dedup)      │
└─────────────────────────────────────────────────────────────────┘
         ▲                    ▲                    ▲
         │                    │                    │
   (cascade)             (cascade)           (set null)
         │                    │                    │
┌─────────────────┐   ┌──────────────────┐  ┌──────────────┐
│  manifests       │   │  validation_     │  │  jobs         │
│    raw BLOB      │   │  history          │  │               │
└─────────────────┘   └──────────────────┘  └──────────────┘


┌─────────────────────────────────────────────────────────────────┐
│  block_list  (independent; matches games by (platform, app_id)   │
│               but NOT a FK — allows pre-blocking unknown apps)   │
└─────────────────────────────────────────────────────────────────┘


┌─────────────────────────────────────────────────────────────────┐
│  cache_observations  (created in 0001 per DQ2; populated only    │
│                        when Post-MVP access-log tail ships)      │
└─────────────────────────────────────────────────────────────────┘
```

### Design choices (with references)

- **DQ8.** `games.platform` FK uses `ON DELETE RESTRICT`. Platform rows are effectively an enum; deleting one is a bug; RESTRICT raises a referential-integrity error rather than cascading a silent mass-delete of every game.
- **DQ3.** `manifests.raw` is a compressed BLOB (zstd on the application side before INSERT). At 2,600 games × ~200 KiB × 3 retained versions ≈ 1.56 GB. Threat-model §4.3 flagged `VACUUM` as the eventual bottleneck; if `VACUUM` > 5 s at 12 months, we revisit DQ3 and move BLOBs to external files.
- **DQ2.** `cache_observations` ships in 0001 even though access-log tail is Post-MVP. Avoids a later schema migration for a planned feature.
- **DQ6.** Mutation responses use the envelope `{"ok", "job_id", "message"}`. `jobs.id` surfaces as `job_id` in API responses; kept as `INTEGER PRIMARY KEY AUTOINCREMENT` for monotonic allocation.
- **No FK from `block_list` to `games`.** Operator can pre-block an app_id the orchestrator hasn't seen yet (F8 acceptance). Block is idempotent on `(platform, app_id)`.
- **No FK from `cache_observations` to anything.** Cache observations are derived from nginx access logs; the orchestrator does not necessarily have a `games` row for every cached hostname. Match at query time if needed.
- **WAL + `PRAGMA synchronous=NORMAL`.** Enabled at migration time. Safe with WAL (no sync-fsync per write), substantial write-throughput gain.
- **Indexes placed for observed query patterns.** Each index in §7 is justified in a comment above its CREATE statement.

---

## 2. Data Isolation & Access Control

**Single-user system.** There is no row-level data isolation between tenants because there is only one tenant. Access-control boundaries are:

1. **Filesystem level.** `state.db` + WAL + SHM files at mode 0600, owned by the non-root container user (UID 1000, Phase 2 Dockerfile decision). State volume mounted read-write into the orchestrator container only; no other container in the compose file sees it.
2. **Process level.** Every `aiosqlite` connection runs inside the orchestrator process; the DB is never opened by a different process, avoiding WAL cross-process coordination risk.
3. **API level.** Every non-health endpoint requires the Docker-secret bearer token; `POST /api/v1/platforms/{name}/auth` additionally requires `127.0.0.1` origin (OQ2). Data access through the API is all-or-nothing — the MVP has no scoped tokens. This is an explicit simplification inherent to single-user scope (threat-model §4.2 item 2).
4. **Query level.** All queries in `db/repository.py` use parameterized SQL via `aiosqlite.Connection.execute(sql, params)`. String concatenation into SQL is rejected by a Semgrep lint rule in CI (per threat-model TM-005 mitigation).

**Future multi-user** would require: per-table `owner_id` column, session-to-owner mapping, row-filter middleware on every query. Out of MVP scope; recorded in threat-model §4.4 as a rewrite-risk trigger.

---

## 3. Sensitivity Controls per Data Element

Cross-referenced with Data Contract §9c (Sensitivity Classification Summary):

| Table.Column | Classification | Control |
|---|---|---|
| `platforms.config` (JSON) | Internal | Contains platform-level settings; no credentials stored here. CI lint rejects writes of known-credential keys (`password`, `refresh_token`, `auth_code`). |
| `games.*` | Internal | Returned by REST API to authenticated clients only. Never logged at INFO+. |
| `games.metadata` (JSON) | Internal | Depot lists, build versions. Could reveal operator library — same sensitivity as `games.title`. |
| `manifests.raw` (BLOB) | Internal | Compressed parsed manifest. Contains chunk SHAs + paths; could reveal depot keys if persisted carelessly — **depot keys are NOT stored**, only chunk digests + file paths. CI test asserts `steam_session.json`-style content does not appear in any `manifests.raw` after a prefill cycle. |
| `block_list.reason` | Internal | Free-text operator note; capped at 500 chars (Data Contract §2.5). |
| `validation_history.error` | Internal | Free-text error from validation. May include file paths and counts; no credentials. |
| `jobs.payload` (JSON) | Internal | Job parameters. `POST /prefill` payload with `{"force": true}` is the maximum — no credentials ever in here. CI lint asserts. |
| `jobs.error` | Internal | Exception repr + correlation_id. Must NOT contain token material — enforced by redaction in exception handler + Semgrep pattern (TM-012). |
| `cache_observations.cache_identifier` | Internal | The `$cacheidentifier` string from nginx (Steam: `"steam"`; Epic: hostname). Not credential material. |

**No PII, no Financial, no Health/Medical, no Regulated data.** Confirmed by threat-model §1.1 and Data Contract §7. Credentials are stored in filesystem session files (mode 0600), not in the DB.

---

## 4. Retention & Pruning

Per Data Contract §6.5, with concrete SQL in §7:

| Table | Policy | Trigger |
|---|---|---|
| `platforms`, `games`, `block_list` | Keep forever | Never pruned |
| `validation_history` | Delete rows where `started_at < datetime('now', '-90 days')` | Daily prune step at end of F12 cycle (before next iteration scheduled) |
| `jobs` | Delete rows where `finished_at < datetime('now', '-90 days') AND error IS NULL` | Same daily prune. `error IS NOT NULL` rows kept indefinitely (post-mortem). |
| `manifests` | Keep latest 3 versions per `game_id`. Delete older. | Weekly prune (during F13 sweep window, which already holds the writer). |
| `cache_observations` | Delete rows where `observed_at < datetime('now', '-30 days')` | Weekly prune; populated only when Post-MVP access-log tail ships, so no-op in MVP. |

Prune SQL is idempotent; failure just means more rows next cycle.

---

## 5. Backup & Recovery

Per Intake §5.4: weekly external `sqlite3 state.db ".backup /backup/..."` cron on the DXP4800 host (outside the container). Backup includes session files.

**The orchestrator itself does not implement backup.** It exposes a restore-friendly state — the DB is always consistent at any moment thanks to WAL. Operator-side:

```bash
# Crontab on DXP4800 host, Sunday 04:30 (after F13 sweep finishes at ~03:30):
30 4 * * 0 sqlite3 /var/lib/docker/volumes/orchestrator-state/_data/state.db \
  ".backup /backup/orchestrator/state-$(date +\%Y\%m\%d).db" && \
  cp /var/lib/docker/volumes/orchestrator-state/_data/*.json /backup/orchestrator/sessions-$(date +%Y%m%d)/
```

Restore is manual: stop container, copy `.db` + session JSONs back to the state volume, start container. Documented in Phase 4 `HANDOFF.md`.

**Phase 4 backup-verification** (not MVP): monthly restore drill on a throwaway volume.

---

## 6. Migration Framework

**Pattern** (copied from Game_shelf's proven design per Brief §5):

- Numbered `.sql` files in `migrations/`: `0001_initial.sql`, `0002_*.sql`, ... with optional `0001_initial_down.sql` for rollback.
- A ~50-LoC Python runner in `db/migrate.py` opens a connection, reads `schema_migrations` table, applies any file with a number greater than `MAX(id)` inside a single transaction per file.
- Migration names follow `NNNN_snake_case_description.sql`.
- **No ORM.** Raw SQL top-to-bottom.

**Meta table** (created by the runner on first boot if `schema_migrations` does not exist):

```sql
-- migrations/_meta_schema_migrations.sql  (bootstrap — runner creates this if missing)
CREATE TABLE IF NOT EXISTS schema_migrations (
    id INTEGER PRIMARY KEY,             -- matches the NNNN prefix of the file
    name TEXT NOT NULL,                 -- the full filename without extension
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checksum TEXT NOT NULL              -- SHA256 of the file contents at apply time
);
```

**Checksum matters.** On every boot, the runner recomputes the SHA256 of each applied migration file and compares to `schema_migrations.checksum`. Mismatch = someone edited an already-applied migration file post-deploy; runner aborts container start with CRITICAL `migration_content_drift`. Migrations are immutable once applied.

**Downgrade handling** — documented in §8: operator-invoked `orchestrator-cli db rollback N` (ships in MVP behind a `--yes-i-know-this-is-destructive` flag). Applies `NNNN_*_down.sql` in reverse order down to version N. Never auto-invoked.

**Version ahead of code** — detected when `MAX(schema_migrations.id) > number_of_files`, meaning operator downgraded the image. Runner aborts with CRITICAL `schema_version_ahead` + guidance to restore from backup or use the matching image version.

---

## 7. `migrations/0001_initial.sql` (canonical)

```sql
-- migrations/0001_initial.sql
-- lancache_orchestrator initial schema
-- Phase 1 Step 1.4 — finalized 2026-04-20
-- References: Brief §5, Data Contract §6, DQ2/DQ3/DQ6/DQ8
--
-- Applied atomically. On success, the runner records
--   INSERT INTO schema_migrations (id, name, checksum) VALUES (1, '0001_initial', '<sha256>');
-- On failure, the whole transaction rolls back and the container refuses to start.

-- ----------------------------------------------------------------------------
-- Pragmas — set at migration time, persist for this DB file.
-- ----------------------------------------------------------------------------
PRAGMA journal_mode = WAL;            -- writers don't block readers
PRAGMA synchronous = NORMAL;          -- safe under WAL, substantially faster
PRAGMA foreign_keys = ON;             -- enforce FK constraints
PRAGMA temp_store = MEMORY;           -- temp tables / indexes in RAM
PRAGMA mmap_size = 268435456;         -- 256 MB mmap for hot pages
PRAGMA cache_size = -32000;           -- ~32 MB page cache

-- ----------------------------------------------------------------------------
-- platforms  —  effectively an enum; 2 rows ever: 'steam', 'epic'.
-- FK from games.platform uses ON DELETE RESTRICT (DQ8).
-- ----------------------------------------------------------------------------
CREATE TABLE platforms (
    name              TEXT PRIMARY KEY CHECK (name IN ('steam', 'epic')),
    auth_status       TEXT NOT NULL CHECK (auth_status IN ('ok', 'expired', 'error', 'never')),
    auth_method       TEXT NOT NULL CHECK (auth_method IN ('steam_cm', 'epic_oauth')),
    auth_expires_at   TIMESTAMP,
    last_sync_at      TIMESTAMP,
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
    last_validated_at     TIMESTAMP,
    last_prefilled_at     TIMESTAMP,
    last_error            TEXT,
    metadata              TEXT,                         -- JSON: depots, build hints
    UNIQUE(platform, app_id)
) STRICT;

-- Fast filter by status (e.g., "all games needing prefill").
CREATE INDEX idx_games_status ON games(status);

-- Covering lookup for library-sync upserts — (platform, app_id) already unique.
-- Named explicitly so we can ADD INDEX IF EXISTS cleanly in future migrations.
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
    fetched_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    blocked_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    started_at         TIMESTAMP NOT NULL,
    finished_at        TIMESTAMP,
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
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    error         TEXT,
    payload       TEXT                                  -- JSON; NEVER contains credentials
) STRICT;

-- Jobs feed and "active jobs" indicator use (state, kind) predicates.
CREATE INDEX idx_jobs_state_kind ON jobs(state, kind);

-- Recent-jobs listing on status page sorts by started_at DESC.
-- (Threat-model §4.3.2 bottleneck mitigation.)
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
    observed_at        TIMESTAMP NOT NULL,
    event              TEXT NOT NULL CHECK (event IN ('hit','miss','expired','revalidated','eviction')),
    cache_identifier   TEXT NOT NULL,                   -- 'steam' or '$http_host'
    path               TEXT NOT NULL,
    bytes              INTEGER CHECK (bytes IS NULL OR bytes >= 0)
) STRICT;

CREATE INDEX idx_co_time ON cache_observations(observed_at DESC);
CREATE INDEX idx_co_event_time ON cache_observations(event, observed_at DESC);
```

**Why `STRICT` tables.** SQLite 3.37+ supports the `STRICT` keyword, which enforces declared column types at write time (a `TEXT` column rejects an int, etc.). Without `STRICT`, SQLite quietly coerces — a silent source of bugs. Python 3.12's bundled SQLite is ≥ 3.40; the Docker base image's `libsqlite3-0` provides 3.37+. CI checks SQLite version at image-build time and fails build if below 3.37.

**Why `CHECK` enumerations instead of lookup tables.** The values are small and fixed (`platform`, `status`, `state`, etc.). Lookup tables would add join cost to every query for zero semantic benefit. Altering an enum set is a migration-file change in either design; CHECK is simpler.

---

## 8. `migrations/0001_initial_down.sql` (rollback)

```sql
-- migrations/0001_initial_down.sql
-- Rollback of 0001_initial.sql
-- Invoked only via: orchestrator-cli db rollback 0 --yes-i-know-this-is-destructive
-- Never auto-run. Operator must acknowledge destructive intent.
--
-- After this runs successfully, the runner also deletes the row from schema_migrations.

-- Drop in reverse dependency order — children first, parents last.
DROP INDEX IF EXISTS idx_co_event_time;
DROP INDEX IF EXISTS idx_co_time;
DROP TABLE IF EXISTS cache_observations;

DROP INDEX IF EXISTS idx_jobs_dedupe;
DROP INDEX IF EXISTS idx_jobs_finished;
DROP INDEX IF EXISTS idx_jobs_started;
DROP INDEX IF EXISTS idx_jobs_state_kind;
DROP TABLE IF EXISTS jobs;

DROP INDEX IF EXISTS idx_vh_started;
DROP INDEX IF EXISTS idx_vh_game;
DROP TABLE IF EXISTS validation_history;

DROP TABLE IF EXISTS block_list;

DROP INDEX IF EXISTS idx_manifests_game_fetched;
DROP TABLE IF EXISTS manifests;

DROP INDEX IF EXISTS idx_games_last_prefilled;
DROP INDEX IF EXISTS idx_games_platform_app;
DROP INDEX IF EXISTS idx_games_status;
DROP TABLE IF EXISTS games;

DROP TABLE IF EXISTS platforms;

-- schema_migrations itself is NOT dropped — it is meta, not versioned.
-- The runner's rollback routine DELETEs from schema_migrations
-- WHERE id = 1 after this script succeeds.
```

**Rollback testing plan (Phase 2 Step 2.6 hard requirement).** Against a realistic-state DB (copied from a full post-Milestone-B state with ~1500 games + 10k jobs), `orchestrator-cli db rollback 0` must:
1. Complete in < 10 s.
2. Leave the file as empty (post-DROP) but with `PRAGMA journal_mode == 'wal'` still set.
3. `orchestrator-cli db migrate` immediately after must re-apply 0001 and bring the DB to empty-schema state (no leftover data).
4. No error logs, no leftover temp files.

---

## 9. Query Patterns (informational — not binding code)

Representative queries the application will issue. Included here so the indexes above can be sanity-checked.

```sql
-- F3/F4 library sync: upsert for an app_id
INSERT INTO games (platform, app_id, title, owned, current_version, size_bytes, metadata)
  VALUES (?, ?, ?, 1, ?, ?, ?)
  ON CONFLICT(platform, app_id) DO UPDATE SET
    title = excluded.title,
    owned = 1,
    current_version = excluded.current_version,
    size_bytes = excluded.size_bytes,
    metadata = excluded.metadata;
-- Uses idx_games_platform_app (via UNIQUE).

-- F12 diff: games needing prefill
SELECT g.platform, g.app_id, g.current_version, g.cached_version, g.status
  FROM games g
  LEFT JOIN block_list b ON b.platform = g.platform AND b.app_id = g.app_id
  WHERE g.owned = 1
    AND b.id IS NULL
    AND (g.current_version != g.cached_version OR g.cached_version IS NULL
         OR g.status IN ('not_downloaded','validation_failed','pending_update'));
-- Uses idx_games_status predicate; LEFT JOIN on block_list uses its UNIQUE.

-- F10 status page: active jobs
SELECT id, kind, game_id, platform, state, progress, started_at
  FROM jobs
  WHERE state IN ('queued', 'running')
  ORDER BY started_at DESC;
-- Uses idx_jobs_state_kind.

-- F9 dedupe check: already-running job for this game+kind?
SELECT id FROM jobs
  WHERE game_id = ? AND kind = ? AND state IN ('queued', 'running')
  LIMIT 1;
-- Uses idx_jobs_dedupe (partial index; tiny).

-- F13 sweep: all cached, not-blocked games
SELECT g.id, g.platform, g.app_id, g.cached_version
  FROM games g
  LEFT JOIN block_list b ON b.platform = g.platform AND b.app_id = g.app_id
  WHERE g.cached_version IS NOT NULL AND b.id IS NULL;

-- Daily prune: old jobs + validation_history
DELETE FROM validation_history
  WHERE started_at < datetime('now', '-90 days');
-- Uses idx_vh_started.
DELETE FROM jobs
  WHERE finished_at IS NOT NULL
    AND finished_at < datetime('now', '-90 days')
    AND error IS NULL;
-- Uses idx_jobs_finished.

-- Weekly prune: keep latest 3 manifest versions per game
DELETE FROM manifests
  WHERE id IN (
    SELECT id FROM (
      SELECT id, ROW_NUMBER() OVER (PARTITION BY game_id ORDER BY fetched_at DESC) AS rn
        FROM manifests
    ) WHERE rn > 3
  );
-- Uses idx_manifests_game_fetched.
```

---

## 10. Connection & Concurrency Model

Per ADR-0001 and threat-model §4.3.1:

- **Single `aiosqlite` connection pool** (size 10, configurable via `DB_POOL_SIZE` env) inside the orchestrator process.
- **`PRAGMA journal_mode = WAL`** enables readers concurrent with a single writer.
- **Application-level write serialization.** A single `asyncio.Lock` in `db/writer.py` wraps every `INSERT/UPDATE/DELETE`. Reduces the WAL-contention surface identified in threat-model §4.3.2 to zero for same-process access. Writers serialize inside the lock; readers proceed unimpeded.
- **`isolation_level = None`** on connections → explicit `BEGIN IMMEDIATE` / `COMMIT` transactions owned by repository functions. Not `DEFERRED` (avoids upgrade-to-write racing).
- **No cross-process access.** The migration runner and the orchestrator both run in the same process; there is no external DB client. The host-side backup cron uses `.backup` which is safe under WAL.

**F13 × F12 overlap scenario** (threat-model §4.3.2 trigger): F13 sweep writes validation_history in batches of 10. Each batch INSERTs ~50 rows inside a single transaction. F12 writes are single-row UPSERTs. With the single-writer lock, F13's batch and F12's UPSERT serialize — no BUSY retries, no silent drops. Worst-case F12 blocks for the duration of one F13 batch (~50 ms on DXP4800 SSD cache), which is imperceptible at API level.

---

## 11. Review Checklist (per Builder's Guide §1.4)

- [x] All entity definitions with relationships — ✅ 7 tables + 1 meta table, FKs documented
- [x] Data isolation / access control strategy — ✅ §2 (single-user simplifications explicit)
- [x] Data sensitivity controls per Phase 0 Data Contract — ✅ §3 cross-reference table
- [x] Versioned, reversible data model changes — ✅ numbered migrations + `.down.sql` rollback + checksum-enforced immutability
- [x] Both "create" and "rollback" operations — ✅ §7 + §8
- [x] Retention / pruning policy defined — ✅ §4 with concrete SQL in §7 query patterns
- [x] Indexes justified against actual query patterns — ✅ §7 comments + §9
- [x] Step 1.4.5 applicability noted — ✅ §1 header (N/A, no legacy data)

---

## 12. Sign-off

**Orchestrator review required.** On approval:
- Migrations `0001_initial.sql` and `0001_initial_down.sql` become canonical; Phase 2 Step 2.6 (initial project setup) copies them verbatim into `migrations/` under the project root.
- `db/migrate.py` runner is a Phase 2 implementation task.
- Rollback testing against realistic state is a Phase 2 Step 2.6 hard requirement (per Builder's Guide §2.6).

**Next Phase 1 step:** 1.5 — Interface Specification (CLI + REST + status-page component states).
