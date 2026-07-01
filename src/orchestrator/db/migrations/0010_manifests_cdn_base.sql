-- 0010_manifests_cdn_base.sql
-- Persist the Epic CDN base path (e.g. /Builds/Org/{catalogId}/{buildId}/default)
-- with each stored manifest. It is stable per game version (only the signed query
-- string is short-lived, and lancache strips it) and is required to compute the
-- Epic lancache cache-key (md5(identifier + cdn_base/chunk_path + slice)) at
-- validate time. Nullable: pre-existing Epic manifests get cdn_base=NULL and are
-- unvalidatable until re-prefilled (the nightly prefill backfills it). Steam
-- manifests leave it NULL (unused). Simple ADD COLUMN — no table recreate.
ALTER TABLE manifests ADD COLUMN cdn_base TEXT;
