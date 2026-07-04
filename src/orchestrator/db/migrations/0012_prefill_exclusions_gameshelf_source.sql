-- 0012_prefill_exclusions_gameshelf_source.sql
-- Piece 3 (#446): Game_shelf pushes cross-launcher coverage exclusions. When an
-- Epic game is already owned (and cached) on a higher-priority launcher (Steam,
-- which self-prefills), the Epic copy is redundant, so Game_shelf POSTs it as an
-- exclude and the orchestrator's Epic scheduled prefill (Piece 2) skips it. Those
-- rows carry source='gameshelf' so the reconcile endpoint
-- (PUT /api/v1/prefill-exclusions/gameshelf/{platform}) can manage exactly its
-- own rows — inserting new ones and deleting stale ones — without ever touching
-- operator or classifier overrides.
--
-- SQLite cannot ALTER a CHECK constraint, so widen source's allowed set with the
-- standard 12-step table rebuild. Statement ORDER matters for the migrate
-- runner's post-apply sanity check: `_expected_tables_for` tracks CREATE TABLE
-- and DROP TABLE names but NOT `ALTER TABLE ... RENAME`. Rename the live table
-- OUT to `_old` first, then CREATE the replacement under its CANONICAL name so it
-- stays in the cumulative expected-tables set (and is present after apply); copy
-- the rows; DROP the `_old` shell (its name was never CREATE'd, so the DROP is a
-- no-op for the expected-set and leaves `prefill_exclusions` expected + present).
ALTER TABLE prefill_exclusions RENAME TO prefill_exclusions_old;

CREATE TABLE prefill_exclusions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL CHECK (platform IN ('steam', 'epic')),
    app_id      TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 64),
    mode        TEXT NOT NULL DEFAULT 'exclude' CHECK (mode IN ('exclude', 'allow')),
    reason      TEXT CHECK (reason IS NULL OR length(reason) <= 500),
    source      TEXT NOT NULL DEFAULT 'classifier'
                CHECK (source IN ('classifier', 'operator', 'gameshelf')),
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, app_id)
) STRICT;

INSERT INTO prefill_exclusions (id, platform, app_id, mode, reason, source, updated_at)
    SELECT id, platform, app_id, mode, reason, source, updated_at
    FROM prefill_exclusions_old;

DROP TABLE prefill_exclusions_old;
