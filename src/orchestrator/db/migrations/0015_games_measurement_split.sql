-- 0015_games_measurement_split.sql
-- Separate cache truth from job outcome (design 2026-09-04).
--
-- games.status previously carried two unrelated meanings: what a measurement
-- found on disk, and how the last job ended. On 2026-09-01 03:00-03:47 an
-- interrupted prefill batch stamped 1769 games with the dead job's outcome, and
-- Epic's scheduled prefill (which selects on status) would have re-downloaded
-- 655 already-cached titles.
--
-- Pure ADD COLUMNs: the status CHECK constraint and its vocabulary are unchanged,
-- so no table rebuild is needed.

ALTER TABLE games ADD COLUMN status_measured_at      TEXT;
ALTER TABLE games ADD COLUMN last_measure_attempt_at TEXT;
ALTER TABLE games ADD COLUMN last_job_outcome        TEXT;
ALTER TABLE games ADD COLUMN last_job_outcome_at     TEXT;

-- Repair. Reset ONLY rows corrupted in the incident window. Legitimately-cached
-- rows also fall inside it (Epic up_to_date runs from 03:30:09), so the predicate
-- excludes them: resetting a good measurement discards information for no gain.
UPDATE games
   SET status = 'unknown',
       status_measured_at = NULL
 WHERE status IN ('validation_failed', 'failed')
   AND last_validated_at >= '2026-09-01 03:00:00'
   AND last_validated_at <= '2026-09-01 03:47:59';

-- Seed the measurement queue ordering. NULL would tie the whole library at the
-- front and fall back to arbitrary id order -- the exact bias being removed.
-- Seeding from last_validated_at puts the June-stamped rows ahead of September's.
UPDATE games SET last_measure_attempt_at = last_validated_at;

-- Truth rows that survived the repair keep their measurement timestamp.
UPDATE games SET status_measured_at = last_validated_at
 WHERE status IN ('up_to_date', 'validation_failed', 'not_downloaded')
   AND last_validated_at IS NOT NULL;

CREATE INDEX idx_games_measure_attempt
    ON games(last_measure_attempt_at);

-- Durable transition log for the circuit breaker (Task 3). An in-memory counter
-- would reset on restart -- and a restart is precisely the scenario that produced
-- the 2026-09-01 corruption, so the count must survive one.
CREATE TABLE measurement_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    prior       TEXT NOT NULL,
    new_status  TEXT NOT NULL,
    downward    INTEGER NOT NULL CHECK (downward IN (0, 1)),
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE INDEX idx_measurement_transitions_window
    ON measurement_transitions(occurred_at DESC) WHERE downward = 1;
