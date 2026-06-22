-- 0008_steam_app_info.sql
-- re-arch ③b: cache of Steam store appdetails (type + name) per app, so
-- library_sync filters prefilled apps to type='game' and names them without
-- re-querying the rate-limited public store API every sync.
CREATE TABLE steam_app_info (
    app_id     TEXT PRIMARY KEY,
    app_type   TEXT NOT NULL,
    name       TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
