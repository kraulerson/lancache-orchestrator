-- 0018_retire_failed_status.sql
-- Retire the unreachable 'failed' cache status (#316).
--
-- 19 owned games carry status='failed' and nothing can ever clear them. No
-- module writes 'failed' any more -- grep finds it only in comments at
-- measurement.py:306 and handlers/validate.py:62 -- and 0015's repair predicate
-- required a last_validated_at inside its window, which these rows do not have,
-- so it passed them by. They ARE re-attempted daily: last_measure_attempt_at
-- updates every sweep. But every attempt returns an error, which correctly takes
-- the attempt-only path and writes no cache truth, so the stale label survives.
--
-- A status that no code can produce and no code can clear is not a state; it is
-- a scar. UAT 15 scenario 5 judged the DISPLAY acceptable, which is why this sat
-- open -- but acceptable-to-look-at is not the same as true, and every future
-- reader of this table has to be told "ignore those 19" out of band.
--
-- WHY 'blocked' AND NOT 'unknown'
--
-- Both are legal in the 0001 CHECK constraint. 'unknown' means "we have not
-- established this yet" -- a state the sweep exists to resolve, and it would put
-- these rows in a bucket that implies pending work forever. 'blocked' means "we
-- are not going to fetch this", which is the actual operational truth, and these
-- rows are already excluded from prefill so the status finally matches the
-- behaviour.
--
-- The operator's instruction was conditional -- migrate them only if they
-- genuinely cannot be downloaded -- so that was verified live on 2026-09-20
-- before this file was written. It holds, by two different mechanisms:
--
--   15 Epic rows fail with 'EpicManifestError: epic manifest API failed: HTTP 4xx'.
--   The titles explain it: The Sims 4, Star Wars Battlefront II and Squadrons are
--   EA App products Epic cannot serve; Super Meat Boy Forever Mobile is a mobile
--   SKU; Unreal Tournament is delisted.
--
--   4 Steam rows fail with 'no_manifest_in_cache' and are absent from
--   SteamPrefill's 1192-app selection, so nothing ever downloads them and no
--   manifest can ever land for the validator to compare against. Three of the
--   four (RPG Maker XP, RPG Maker VX Ace, Lossless Scaling) are tools, not games.
--
-- NOT PREDICATED ON status_measured_at
--
-- All 19 live rows have status_measured_at IS NULL, so filtering on it would
-- work today. It is deliberately NOT filtered on, because that is precisely the
-- mistake 0015's repair made: it predicated on a column these rows happened to
-- lack, silently skipped them, and the defect surfaced in UAT weeks later.
-- 'failed' is unreachable for every row regardless of how it got there.
--
-- Ownership is likewise not filtered. A row left behind because it was unowned
-- on migration day would become a fresh instance of this same bug the moment
-- library_sync re-owned it.

-- The audit row comes FIRST: it reads the prior status, so it must run while
-- that status is still 'failed'.
--
-- commanded = 1 is load-bearing. 0016 added that flag so a deliberate operator
-- action does not sit in the circuit breaker's rolling window as though it were
-- cache loss (#310). A migration is the most deliberate action there is, and 19
-- downward transitions arriving in one instant would otherwise be read as an
-- incident and could veto the next honest sweep's first measurement.
--
-- downward = 1 is honest rather than convenient: failed -> blocked removes these
-- games from any future prospect of being cached. Recording 0 would make the
-- breaker's exclusion automatic, but it would put a falsehood in the one log a
-- future investigator reads. The commanded flag, not a dishonest direction, is
-- what keeps this out of the incident count.
INSERT INTO measurement_transitions (game_id, prior, new_status, downward, commanded)
SELECT id, status, 'blocked', 1, 1
FROM games
WHERE status = 'failed';

-- status_measured_at is deliberately left untouched, which for these rows means
-- left NULL. 'blocked' is a policy decision, not a cache observation; stamping a
-- measurement time would assert we inspected the cache and found it in this
-- state. Epic's API refusing us is not an observation of the cache. Conflating
-- the two is the exact failure migration 0015 was written to end.
--
-- last_error is also preserved. It is the evidence for why each row was blocked,
-- and discarding it would leave a future reader with a terminal status and no
-- account of how it was reached.
UPDATE games
SET status = 'blocked'
WHERE status = 'failed';
