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
