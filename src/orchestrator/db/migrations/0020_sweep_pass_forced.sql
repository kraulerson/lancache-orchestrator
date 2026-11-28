-- 0020_sweep_pass_forced.sql
-- Feature B (eviction re-check): a cache eviction can restart the sweep pass.
--
-- When cache-catcher detects an uncommanded eviction, the control plane restarts
-- the pass at the eviction time so every game measured before it is measured
-- again, and keeps a sweep queued until that pass completes.
--
--   forced            1 while such an eviction-forced pass is in flight; the
--                     eviction watch re-enqueues a sweep whenever none is queued
--                     or running. complete_pass() clears it.
--   last_eviction_ts  the newest eviction event (ISO-8601 UTC, cache-catcher's
--                     format) the control plane has acted on, so a restart never
--                     acts on the same eviction twice. Survives pass completion.
--
-- Additive only: the existing row keeps its pass and gets forced=0, NULL ack.

ALTER TABLE sweep_pass ADD COLUMN forced INTEGER NOT NULL DEFAULT 0 CHECK (forced IN (0, 1));
ALTER TABLE sweep_pass ADD COLUMN last_eviction_ts TEXT;
