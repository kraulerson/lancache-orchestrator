-- 0017_sweep_pass.sql
-- Give the validation sweep a pass boundary that outlives a single run (#311).
--
-- A full pass costs about 12.5 h: 8.1 TiB of manifest-backed cache at the
-- ~650 GiB/h the NAS sustains. `job_max_runtime_sec` is 6 h. The sweep handler
-- re-queried all 3212 candidates on every run with no concept of a pass, so it
-- could only ever emit `sweep.completed` by covering the whole library inside
-- one 6 h window — arithmetically impossible. 15 of 16 sweeps since migration
-- 0015 were recorded `failed`, the Uptime Kuma sweep monitor could never go
-- green, and nothing proved the library had been covered end to end.
--
-- Ordering by last_measure_attempt_at already makes an interrupted sweep resume
-- at the frontier, so no data was ever at risk and no game could starve. What
-- was missing was only the BOUNDARY: a fixed point to measure coverage against.
-- This table is that point. Candidates become "not yet attempted since this
-- pass began" rather than "least recently attempted", so an empty candidate set
-- is a proof of coverage rather than a coincidence.
--
-- One row, enforced by the primary key CHECK. There is exactly one pass in
-- flight at a time; a schema that cannot express a second one cannot drift into
-- expressing one by accident, and "the current pass" can never be ambiguous.

CREATE TABLE sweep_pass (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    pass_number     INTEGER NOT NULL CHECK (pass_number >= 1),
    pass_started_at TEXT    NOT NULL
);

-- Seeded here rather than initialised on first read: an initialise-on-read path
-- would be a second place a pass can begin, running under whatever concurrency
-- the sweep happens to have. This is the only one.
--
-- CURRENT_TIMESTAMP seeds pass 1 as starting NOW, which makes every existing
-- game a candidate — the live DB has 3212 owned games and no NULL
-- last_measure_attempt_at, so a stamp any earlier would let pass 1 "complete"
-- within minutes without measuring a thing.
INSERT INTO sweep_pass (id, pass_number, pass_started_at) VALUES (1, 1, CURRENT_TIMESTAMP);

-- The candidate query filters on games.last_measure_attempt_at and orders by it;
-- idx_games_last_measure_attempt_at (0015) already covers that access path.
