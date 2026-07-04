-- 0011_prefill_exclusions.sql
-- #225/#366: auto-exclude non-games from the SCHEDULED prefill AFTER they have
-- been downloaded once. The scheduled prefill keeps downloading everything; the
-- auto-classify step then flags soundtracks / tools / SDKs / dedicated servers /
-- demos (via the #229 selection classifier over steam_app_info) and inserts an
-- 'exclude' row here, so the NEXT prefill cycle skips them. One-time download of
-- a non-game is accepted (operator decision 2026-07-04).
--
-- `mode='allow'` is the operator's STICKY override: the auto step inserts with
-- ON CONFLICT(platform, app_id) DO NOTHING, so it never overrides an operator
-- 'allow' — an un-excluded game stays cached and is never re-flagged.
CREATE TABLE prefill_exclusions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL CHECK (platform IN ('steam', 'epic')),
    app_id      TEXT NOT NULL CHECK (length(app_id) BETWEEN 1 AND 64),
    mode        TEXT NOT NULL DEFAULT 'exclude' CHECK (mode IN ('exclude', 'allow')),
    reason      TEXT CHECK (reason IS NULL OR length(reason) <= 500),
    source      TEXT NOT NULL DEFAULT 'classifier'
                CHECK (source IN ('classifier', 'operator')),
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, app_id)
) STRICT;
