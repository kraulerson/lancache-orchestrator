# UAT 15 — Exploratory agent (persona: Malicious User)

**Verdict: 5 real defects. Two SEV-2, three SEV-3.** All CONFIRMED items were executed;
throwaway tests were run from the scratchpad. No tracked file modified.

## SEV-2 — a purge that deletes NOTHING still writes commanded cache truth, breaker-exempt
`src/orchestrator/jobs/handlers/purge.py:135-160` + `src/orchestrator/agent/routers/steam.py:575-583`

The agent returns HTTP 200 with `{"deleted": 0, "failed": N}` when every unlink fails.
`purge_handler` reads `files_failed` at line 136 and **never acts on it** — it unconditionally
runs `record_measurement(..., "partial", tx=tx, commanded=True)` and `_record_cache_emptied`
(chunks_cached = 0).

Scenario: the agent comes back at uid 1000 instead of `0:0` — the documented, twice-recurring
fault in this project (65/256 buckets readable). Every unlink returns EACCES. **The cache on
disk is fully intact.** The control plane writes `status='validation_failed'`,
`status_measured_at=now`, and a history row claiming 0/N cached. Because `commanded=True`,
the circuit breaker — the mechanism built for exactly "the agent is lying about what it read" —
is skipped by construction and no transition row is written, so a bulk purge of an intact
library produces **zero breaker signal** and queues the whole set for re-prefill.

Fix direction: treat `files_failed > 0 and files_deleted == 0` as a failed purge (raise, leave
status untouched). A purge is only known truth if the delete actually happened.

## SEV-2 — one `error` history row permanently pins a large Steam game to the 300s base budget
`src/orchestrator/validator/disk_stat.py:358-363` with `src/orchestrator/jobs/handlers/validate.py:66-79`

The chunk-scaled budget reads `chunks_total` from `ORDER BY id DESC LIMIT 1` — the newest row
regardless of outcome. `validate_one_game` writes a history row unconditionally and an `error`
outcome carries `chunks_total = 0`, which `int(last_total) if last_total else None` turns into
`None`.

Measured:
```
chunk_count before error row = 359671  (budget 9292s)
chunk_count after  error row = None    (budget  300s)
```
It does not recover. At 300s the game times out, the httpx timeout surfaces as `AgentError`,
which is not caught in `disk_stat.py` and propagates out of `validate_one_game` BEFORE the
history insert — so no new row is written and the poisoned 0-total row stays newest forever.
One transient "cache not mounted" moment permanently un-validatable-ises exactly the games
PR #303 was raised to rescue.

Fix direction: `SELECT chunks_total ... WHERE chunks_total > 0 ORDER BY id DESC LIMIT 1`.

Related (PLAUSIBLE): a large Steam game with no successful history ever gets 300s, times out,
writes no row, and can never bootstrap a budget.

## SEV-3 — nine writer-guard evasions, seven valid SQLite
`tests/test_measurement_writer_guard.py:36-53`. Each form was separately executed against
sqlite 3.53.4 and confirmed to actually change `games.status`:

1. `REPLACE INTO games (id, status) VALUES (?,?)` — guard requires the literal `INSERT`
2. `UPDATE games AS g SET status=...`
3. `UPDATE OR REPLACE games SET status=...`
4. `UPDATE games SET size_bytes=(SELECT s FROM sizes WHERE gid=games.id), status='up_to_date' WHERE id=?`
   — the `(?!\bWHERE\b)` body guard aborts on the SUBQUERY's WHERE
5. `UPDATE games SET last_error='a;b', status='up_to_date' ...` — `;` excluded from the body class
6. `UPDATE games SET "cached_version"=?, status=? ...` — a double-quoted identifier hides the rest
7. `"UPDATE games SET " + "status=? WHERE id=?"` — explicit `+`; `_LITERAL_SEAM` only collapses
   implicit adjacency
8. `f"UPDATE games SET {col}=? WHERE id=?"` — interpolated column name
9. triple-quoted INSERT with a quoted column

None exist in the tree today, so this is latent regression risk. **4, 5 and 6 are the dangerous
ones** — ordinary SQL a future author would write with no intent to evade.
Hardening: drop `"` and `;` from the body class, cut the body at the first top-level `WHERE`
after balancing parentheses, add `REPLACE\s+INTO` and `(?:OR\s+\w+\s+)?games(?:\s+AS\s+\w+)?`
to the heads. Also unscanned and unstated: `scripts/**/*.py` and
`src/orchestrator/db/migrations/*.sql` (0015 itself does `UPDATE games SET status`).

## SEV-3 — a Kuma push that never arrives burns the dedupe stamp; the incident goes unannounced
`src/orchestrator/jobs/measurement.py:104-122, 231-240`, `src/orchestrator/clients/heartbeat.py:56-58`

`_breaker_notice_due()` stamps `_last_breaker_notice_at` BEFORE the push is attempted, and
`heartbeat.push` swallows every failure internally so `_notify_breaker`'s except never fires.
Measured: 2 trips, 1 push attempt, second silent. Compounding: `sweep.py:132-149` aborts on the
FIRST trip, so a sweep makes exactly one push attempt — and a NAS/network fault is precisely
correlated with mass eviction. Later sweeps in the 60-minute window log only
`measurement.breaker_refused` at INFO; `sweep.aborted` at ERROR is the only remaining trace.
Fix: stamp only on a delivery the push reports successful, or make `heartbeat.push` return bool.

Minor, already acknowledged in the docstring: a refused write inserts no transition row, so every
refusal reports the identical `in_window` count — measured `['5','5','5','5','5','5']`. The log
can never tell the operator how large the incident is.

## SEV-3 — a cancelled sweep's attempt-writes are orphaned and land after the job is marked timed-out
`src/orchestrator/jobs/handlers/sweep.py:125-131, 183`

`asyncio.gather` without `return_exceptions` propagates the first child `CancelledError` and does
not wait for the rest. At `sweep_batch_size=2` the queued games are cancelled at `sem.acquire()`
— OUTSIDE the try — so they raise instantly, gather completes, and the two in-flight `_one()`
tasks are abandoned mid-`_record_attempt`. Measured (4 games, 0.1s budget): handler raised at
0.10s; attempts stamped at that moment = 0, one second later = 2. The worker marks the job failed
and claims the next job while orphaned tasks are still writing through the single writer lock.

Mirror case: with only in-flight games, gather DOES wait, and a write that never completes means
`asyncio.wait_for(handler, job_max_runtime_sec)` never returns — the job budget is not a hard
bound. Bounded in production by `pool_busy_timeout_ms=5000` per write, so seconds of overrun,
not a permanent wedge. Fix: `asyncio.shield` the attempt write with its own short timeout, or
gather with `return_exceptions=True` and re-raise after the children settle.

## Attacked and found clean
- **`commanded=True` reachability** — `purge.py:160` is the only caller; no request or CLI input
  flows into the flag. The bypass mechanism itself is correct. The defect above is the
  precondition, not the mechanism.
- **`validate_timeout_for` fuzzing** — `None/0/-1/True/5e6/2**63-1` all behave; `chunks_per_sec`
  of `0/-1/1e-9` all safe. `OverflowError` needs `chunk_count > 2**1024`, unreachable past
  int64 and the agent's `_MAX_CHUNKS = 5_000_000`. `nan` rejected by pydantic `gt=0`.
- **Clock skew on the dedupe** — `time.monotonic()` cannot go backwards; the stamp starts `None`
  after a restart. Neither a restart nor a wall-clock change produces a storm or permanent silence.
- **Concurrent sweeps sharing the module-level stamp** — correct in-process, correctly independent
  across processes.

## Brief error on my part
I told this agent that four guard blind spots were documented in `.superpowers/sdd/task-9-report.md`.
That file is the Epic validation parity report and lists none. The agent said so rather than
inventing a diff, and enumerated nine from scratch.
