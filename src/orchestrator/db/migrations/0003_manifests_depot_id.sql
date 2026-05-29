-- 0003_manifests_depot_id.sql
-- F7: add depot_id so the cache validator can build Steam chunk URLs
-- (/depot/<depot_id>/chunk/<sha>) and pick the latest manifest per depot.
--
-- Nullable INTEGER — no backfill needed (no live manifest data exists
-- yet; the BL12 manifest_fetch handler populates it going forward). A
-- bare ADD COLUMN of a nullable column is STRICT-safe and does not
-- require a table rebuild.

ALTER TABLE manifests ADD COLUMN depot_id INTEGER;

-- Supports "latest manifest per depot for a game" lookups in F7.
CREATE INDEX idx_manifests_game_depot
    ON manifests(game_id, depot_id, fetched_at DESC);
