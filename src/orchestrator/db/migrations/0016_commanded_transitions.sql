-- 0016_commanded_transitions.sql
-- Tell a deliberate cache change apart from unexplained loss (#310).
--
-- Until now a purge wrote NO measurement_transitions row at all. That kept the
-- circuit breaker quiet, but at the price of omitting the largest deliberate
-- cache change the system can make from the immutable log that exists to record
-- exactly that. #310 moves the purge onto a real post-purge measurement, so the
-- row is now worth keeping.
--
-- It cannot simply be kept unmarked. The breaker counts downward rows in a
-- rolling window, so a library-sized purge would sit in that window and refuse
-- the NEXT sweep's first honest measurement — a false alarm the operator caused
-- themselves, which is how a breaker stops being trusted.
--
-- So the row is written AND marked, and both the breaker's count and its
-- supporting index exclude marked rows.
--
-- Pure ADD COLUMN plus an index swap: no table rebuild, no data rewrite.

ALTER TABLE measurement_transitions
    ADD COLUMN commanded INTEGER NOT NULL DEFAULT 0 CHECK (commanded IN (0, 1));

-- Every row written before this migration was an observation, never a command —
-- which is exactly what the DEFAULT 0 gives them.

-- The breaker's window query is index-backed, and its predicate has changed.
-- Leaving the old index in place would make a bulk purge fill the very structure
-- the breaker scans, forcing it to filter those rows out by hand on every
-- measurement.
DROP INDEX IF EXISTS idx_measurement_transitions_window;
CREATE INDEX idx_measurement_transitions_window
    ON measurement_transitions(occurred_at DESC)
    WHERE downward = 1 AND commanded = 0;
