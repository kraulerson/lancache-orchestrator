-- 0013_steam_app_info_categories.sql
-- MP-only detection (#366, Karl 2026-07-04: exclude multiplayer-only games from
-- prefill). A game Steam types as `game` can still be undesirable to cache if it
-- has NO single-player mode (e.g. Dota 2) — the type/name classifier can't see
-- that. Store the Single-player / Multi-player category signals (from the store
-- appdetails `categories` list, fetched by library_sync) so classify() can flag
-- a game that has a multiplayer category and no single-player category.
--
-- Both columns are nullable INTEGER (0/1) with NULL = "categories not yet
-- fetched" — the classifier never guesses MP-only from an unknown flag. Plain
-- ADD COLUMN (no table rebuild): existing rows get NULL, and library_sync
-- backfills them on subsequent syncs (budget-bound).
ALTER TABLE steam_app_info ADD COLUMN has_single_player INTEGER;
ALTER TABLE steam_app_info ADD COLUMN has_multiplayer INTEGER;
