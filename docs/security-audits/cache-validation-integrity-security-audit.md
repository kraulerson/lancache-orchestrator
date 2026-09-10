# Security Audit — Cache validation integrity (measurement / job-outcome split)

**Feature:** cache-validation-integrity (split cache truth from job outcome; single measurement writer; durable circuit breaker; unfiltered least-recently-attempted sweep)
**Modules:**
- `src/orchestrator/jobs/measurement.py` — new; `record_measurement()` / `record_job_outcome()`, the only writer of `games.status` / `games.status_measured_at`
- `src/orchestrator/db/migrations/0015_games_measurement_split.sql` — four new `games` columns, the 2026-09-01 repair, the ordering backfill, `measurement_transitions`
- `src/orchestrator/jobs/handlers/sweep.py` — unfiltered candidate SQL, attempt stamping, breaker abort
- `src/orchestrator/jobs/handlers/validate.py`, `prefill.py`, `purge.py`, `library_sync.py` — truth writes removed / routed
- `src/orchestrator/jobs/worker.py`, `src/orchestrator/jobs/reaper.py` — timeout and boot paths write job outcome only
- `src/orchestrator/scheduler/jobs.py` — Epic scheduled-prefill evidence predicate
- `src/orchestrator/core/settings.py` — `measurement_breaker_threshold`, `measurement_breaker_window_minutes`, `kuma_push_measurement_breaker`
- `tests/test_measurement_writer_guard.py` — source-scanning build guard

**Audit date:** 2026-09-07 (re-verified 2026-09-07 after remediation `c50399c`)
**Auditor:** Claude Fable 5.1 (Senior Security Engineer persona) + ruff S + mypy --strict + semgrep
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-09-07 -->

## Scope

Branch `docs/cache-validation-integrity-design`, merge-base `fd52ca2` → head `c50399c` (14 commits, 39 files, +3481/−284). The findings below were raised against `180f2b8`; `c50399c` is the remediation commit, re-verified in the section at the end. `games.status` previously carried two unrelated meanings — what a measurement found on disk, and how the last job ended — and on 2026-09-01 an interrupted prefill batch stamped 1769 games with a dead job's outcome, which the Epic scheduled prefill would have acted on as 655 downloads. This branch makes that structurally impossible: one writer, a durable transition log, a circuit breaker on mass downward movement, and a sweep that selects on `owned` and ordering rather than on status.

Not in scope: the Game_shelf follow-ups named in the design doc (separate repo), and the operational rollout steps (deploy, `dpa-pre-N` tag).

## Methodology

1. **SAST.** `.venv/bin/ruff check --select S src/orchestrator tests` (flake8-bandit) — *All checks passed*. `.venv/bin/mypy --strict src/` — *Success: no issues found in 109 source files*. `git diff --name-only fd52ca2..180f2b8 -- '*.py' | tr '\n' '\0' | xargs -0 semgrep scan --config=p/owasp-top-ten --config=.semgrep/ --quiet --no-git-ignore` — *159 rules, 33 targets, 0 findings, 0 errors* (semgrep 1.x at `/opt/homebrew/bin/semgrep`).
2. **Diff read.** Whole-branch diff read in full from `.superpowers/sdd/2026-09-04-cache-validation-integrity/review-fd52ca2..180f2b8.diff`, plus the design doc's Design and Prevention sections, then each changed source file read at head.
3. **Live probes** against real migrated SQLite DBs and the real `Pool`, `purge_handler` and `validate_one_game`: the migration repair predicate against ten hand-seeded edge rows; `EXPLAIN QUERY PLAN` on the breaker count and the sweep candidate SQL; pydantic rejection of poisoned breaker settings; the writer-guard regex against seven evasion shapes and against timing payloads; and two end-to-end exploit probes reproducing findings 1 and 2.
4. **Focused tests.** `tests/agent/test_import_isolation.py tests/test_measurement_writer_guard.py tests/jobs/test_measurement.py tests/jobs/test_measurement_circuit_breaker.py tests/db/test_migration_0015_measurement_split.py` — 32 passed. `tests/jobs/handlers/test_sweep_timeout_safety.py tests/jobs/handlers/test_sweep_ordering.py tests/jobs/test_sweep_handler.py tests/jobs/test_purge_handler.py tests/jobs/test_purge_records_cache_state.py tests/scheduler/test_scheduled_prefill_evidence.py` — 41 passed. (Full suite not run per audit instruction.)
5. **Re-verification pass (`c50399c`).** All three SAST tools re-run at the remediation head; every fixed finding re-probed with the *original* exploit probe rather than the new tests, so a passing assertion could not stand in for a closed exploit; the one consequence the remediation introduced measured with its own probe. Detail in the Re-verification section below.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| 1 | SEV-2 | A breaker-tripped purge deletes the files and records nothing — the game stays green with no data behind it | **Fixed in `c50399c`** |
| 2 | SEV-3 | A breaker-refused validate still persists its `validation_history` row, so status and chunk counts diverge | **Fixed in `c50399c`** |
| 3 | SEV-3 | Migration 0015 manufactures the "positive evidence of absence" that authorises Epic downloads | **Accepted (controller ruling 2026-09-07)** |
| 4 | SEV-4 | The source-scanning writer guard has six confirmed evasions | **Partly fixed in `c50399c`; 4 of 6 Deferred (documented)** |
| 5 | SEV-4 | `kuma_push_measurement_breaker` is a credential-bearing plain `str` the log redactor does not recognise | **Fixed in `c50399c`** |
| 6 | SEV-4 | `measurement_transitions` grows without bound; no retention path exists | **Deferred (follow-up)** |
| 7 | SEV-4 | The breaker's Kuma push now runs inside validate's transaction and stalls every writer in the process for up to 10s | **Accepted (controller ruling 2026-09-07)** — new, introduced by the fix for #2 |

---

### 1 — SEV-2: A breaker-tripped purge deletes the files and records nothing

**Where.** `src/orchestrator/jobs/handlers/purge.py:136` (`agent.steam_purge` — the files are irreversibly unlinked here) then `purge.py:157-159`, where `record_measurement(..., tx=tx)` and `_record_cache_emptied(..., tx)` run inside one `write_transaction`. `record_measurement` checks the breaker at `measurement.py:203-237` and raises **before** any write, so the exception escapes the `async with`, the transaction rolls back, and *both* the status flip and the compensating `validation_history` row are discarded — after the delete has already happened.

**Concrete failure.** The breaker's count is global across every source of downward transitions, so this needs no mass purge. A real lancache eviction event — the exact scenario this feature exists to detect, and one this project has already lived through (2026-07-31, nginx cache-manager evicting ~31 objects/s) — leaves the sweep having recorded 24 downward transitions inside the 60-minute window. The operator then purges **one** game to force a clean re-prefill. That single purge is the 25th downward transition:

```
pre-existing downward transitions in window: 24  (threshold=25)
purge.started                  game_id=1 job_id=1 platform=steam
measurement.breaker_tripped    downward_in_window=25 prior=up_to_date new_status=validation_failed
pool.transaction_rolled_back   role=writer
purge_handler raised CircuitBreakerTripped: 25 games lost cache state within 60 minutes; writing halted
agent purge calls (files actually deleted): [730]
DB after:  status='up_to_date'  status_measured_at=None
           newest validation_history: 337/337 cached
API/Game_shelf would render: badge from status='up_to_date', chunks 337/337
```

(Probe: real `Pool` over a migrated temp DB, real `purge_handler`, stub agent that records the delete. Reproduced verbatim.)

The result is the UAT-14 #293 defect resurrected — a game rendering a green **Cached 337/337** badge with no files on disk — but reached deterministically instead of by a crash, and now with three aggravating factors the original did not have:

- **The correction path is disabled by the same breaker.** The design's answer ("the next sweep measures the real state") is exactly what cannot happen: the next sweep's `up_to_date → not_downloaded` write for this game is itself downward, so it is refused too, and `sweep.py:132-146,201` aborts the whole sweep on the first trip. The divergence persists until the window clears *and* a sweep reaches this row — on a library the design itself says takes days to measure, that is not bounded by hours.
- **Re-prefill will not rescue it.** The row keeps `status='up_to_date'`, so the new Epic predicate at `scheduler/jobs.py:180-181` excludes it permanently. The game is never re-downloaded; the first person to launch it pulls the whole title from the WAN at full rate.
- **The job reports the opposite of what happened.** `CircuitBreakerTripped` propagates to `worker.py`, the purge job is marked `failed`, and Game_shelf's purge poll shows a failure. An operator reading "purge failed" will reasonably conclude the files are still there. They are not.

**Severity rationale.** Silent, durable divergence between authoritative on-disk state and the DB; triggered by ordinary operator action during precisely the incident the feature targets; no attacker required; the designed self-healing mechanism is inert; and it re-opens a defect a human tester previously refused to pass.

**Fix.** A purge is a *commanded* state change with an authoritative cause — it is ground truth, not a measurement whose trustworthiness is in question. The breaker exists to catch an agent that is lying about what it read; it should not be able to veto a write about files this system itself deleted. Give `record_measurement` a `commanded: bool = False` (or a sibling `record_commanded_change`) that still writes the transition row (so the purge's contribution to a genuine mass-loss alarm is counted and visible) but never raises, and pass it from `purge.py:158`. Add a test that seeds `threshold - 1` downward transitions and asserts a purge still records both the status flip and the `validation_history` row.

**Status: Fixed in `c50399c`.** `record_measurement` gained `commanded: bool = False` (`measurement.py:157`); the breaker check is now `if downward and not commanded` (`measurement.py:219`) and `purge.py:160` passes `commanded=True`. The exploit probe re-run against `c50399c` is fully closed — see Re-verification below. The implementation departs from the fix I proposed in one respect: a commanded change writes **no** `measurement_transitions` row (`measurement.py:265-270`), logging `measurement.commanded` instead. I accept that ruling; see the note under Re-verification.

---

### 2 — SEV-3: A breaker-refused validate still persists its `validation_history` row

**Where.** `src/orchestrator/jobs/handlers/validate.py:47-59` writes the `validation_history` row unconditionally and outside any transaction; `validate.py:67` then calls `record_measurement`, which may raise.

**Concrete failure.** `measurement.py:168-170` states that a tripped breaker "persists nothing at all, so the library keeps the last state a trustworthy measurement gave it." That is not what happens — the observation lands, only the status does not. `api/routers/games.py:95-98` serves `chunks_cached` / `chunks_total` from the newest `validation_history` row alongside `status`, and Game_shelf's badge keys on `status` while its percentage comes from those chunk fields. Probe (real pool, real `validate_one_game`, `validate_game` stubbed to return `partial 17/337`, 24 downward transitions pre-seeded):

```
raised: 25 games lost cache state within 60 minutes; writing halted
games.status = 'up_to_date'
newest validation_history = 17/337 outcome='partial'
=> API returns status='up_to_date' alongside chunks 17/337
```

The operator investigating the breaker alarm reads a green badge on a game whose own latest observation says 5% cached — on the surface they are using to diagnose the alarm.

**Bound.** Limited to games already inside `validate_one_game` when the trip occurs. `sweep.py:87` sizes the semaphore at `sweep_batch_size` (default 10) and `sweep.py:118-123` short-circuits everything queued behind it, so ≤ `sweep_batch_size` rows per trip. Self-corrects on the next successful measurement of each row.

**Fix.** Move the `validation_history` insert after `record_measurement`, or wrap both in one `write_transaction` the way `purge.py:157-159` already does. The latter is preferable — it makes validate and purge structurally identical and closes the same #293 class in both.

**Status: Fixed in `c50399c`.** `validate_one_game` now opens `async with pool.write_transaction() as tx`, writes `_INSERT_VH` via `tx.execute`, and calls `record_measurement(pool, game_id, result.outcome, tx=tx)` inside it (`validate.py:60-90`) — the preferred option, so validate and purge are now structurally identical. A refusal rolls both back; probe confirms the history row is gone. One consequence is accepted by ruling and graded separately as finding 7.

---

### 3 — SEV-3: Migration 0015 manufactures the evidence that authorises Epic downloads

**Where.** `0015_games_measurement_split.sql:34-36`:

```sql
UPDATE games SET status_measured_at = last_validated_at
 WHERE status IN ('up_to_date', 'validation_failed', 'not_downloaded')
   AND last_validated_at IS NOT NULL;
```

**Concrete failure.** Design decision 3 is "Never download without positive evidence of absence," and `scheduler/jobs.py:180-181` implements it as `status IN ('validation_failed','not_downloaded') AND status_measured_at IS NOT NULL` — the docstring at `scheduler/jobs.py:174-178` reads "A download now requires a real measurement that actually found the game absent or incomplete." This UPDATE grants that evidence retroactively to every pre-existing `validation_failed` / `not_downloaded` row from a column the design explicitly does not treat as measurement evidence, without any post-deploy measurement. The design's Repair section specifies only two UPDATEs (the incident reset and the `last_measure_attempt_at` seed); this third one is an implementation addition.

Failure path: deploy 0015 onto a host where `ORCH_SCHEDULED_PREFILL_ENABLED` is `true` — which is the value `/root/orch-lxc.env` held until 2026-09-04, and `deploy-orchestrator-lxc.sh` passes only `--env-file`, so a redeploy restores it. On the next tick (`45 3,9,15,21`) every owned Epic game with a stale non-NULL `last_validated_at` and one of those two statuses queues a prefill, before any sweep has measured anything.

**Mitigating.** The predicate is strictly *narrower* than the `status <> 'up_to_date'` it replaces, so this is never a regression against pre-branch behaviour; the 1769 genuinely corrupted rows *are* excluded (they are reset to `unknown` / NULL by `0015:21-26`, verified below); prefill is disarmed operationally; and re-prefilling through lancache is largely a HIT, so the cost is CPU/disk churn on a CPU-constrained host rather than a WAN storm. Hence SEV-3, not SEV-2 — but the invariant is asserted in the docstrings more strongly than the migration delivers.

**Fix.** Restrict the backfill to `status = 'up_to_date'` (a status that is not download-eligible, so the seed cannot authorise anything), and let `validation_failed` / `not_downloaded` rows earn `status_measured_at` from the first post-deploy sweep. If the ordering benefit of seeding those rows is wanted, it is already provided by `last_measure_attempt_at` at `0015:31`, which is a separate column and does not gate downloads.

**Status: Accepted (controller ruling 2026-09-07).** Kept as written, on three grounds: the statement is in the Karl-approved implementation plan; for the three surviving truth statuses `last_validated_at` genuinely *is* the timestamp of the last real measurement, so the backfill is not fabricating a value so much as relocating one; and rollout step 3 re-measures the whole library before step 4 re-enables Epic downloading, so no row reaches the prefill predicate on a migration-seeded timestamp alone. I record the ruling as reasonable and the residual as procedural rather than structural: the safety now depends on the rollout order being followed and on `ORCH_SCHEDULED_PREFILL_ENABLED` staying false until step 4, rather than on the predicate being unable to fire. Worth a line in the deploy runbook.

---

### 4 — SEV-4: The writer guard has six confirmed evasions

**Where.** `tests/test_measurement_writer_guard.py:36-41`. `CACHE_TRUTH_WRITE` matches `UPDATE games SET` / `DO UPDATE SET`, then any run of characters that are not `"` or `;` and do not begin a `WHERE`, then `status=` / `status_measured_at=`.

**Concrete failure.** Probed each shape through the guard's own `writes_cache_truth()`; `True` = caught:

```
caught=True  plain truth write (control)
caught=False UPDATE games SET last_error=(SELECT e FROM t WHERE t.id=games.id), status='failed' WHERE id=?
caught=False INSERT INTO games (platform, app_id, status) VALUES ('epic', ?, 'up_to_date')
caught=False INSERT OR REPLACE INTO games (id, status) VALUES (?, 'unknown')
caught=False col = "status" ; sql = f"UPDATE games SET {col}=? WHERE id=?"
caught=False "UPDATE games SET last_error='a;b', status='failed' WHERE id=?"
caught=False "UPDATE games SET " + col_expr + " status=? WHERE id=?"
```

The first is the sharpest: the negative lookahead `(?!\bWHERE\b)` cannot tell a subquery's `WHERE` from the statement's own, so any correlated subquery ahead of the truth column blinds the guard. The second and third matter because `library_sync.py:47,62` already inserts into `games`; adding `status` to one of those column lists writes cache truth for a new row with no `UPDATE` anywhere. This is a preventative control, not a runtime one — nothing is exploitable today, and it does catch every form present in the branch (scan over `src/orchestrator`: 109 files, 43.6 ms, exactly one hit, `jobs/measurement.py`, which is in `ALLOWED`) — but the design leans on it as *the* structural guarantee, so its edges should be honest.

**Fix.** Add `INSERT (OR ...)? INTO games` with `status` in the column list as a second pattern; stop the body at `\bWHERE\b` only when the parenthesis depth is zero, or simply drop the lookahead and accept the false positive on the reaper (whose statement can be excluded by name). Extend `_MUST_MATCH` with these six shapes.

**Status: partly fixed in `c50399c`; the remaining four are Deferred (documented).** A second pattern `CACHE_TRUTH_INSERT` (`tests/test_measurement_writer_guard.py:44-53`) now matches `INSERT (OR <verb>)? INTO games (…status…)`, and `writes_cache_truth` ORs the two. Re-probed: the two INSERT forms are caught, `INSERT OR IGNORE INTO games (id, status_measured_at)` is caught too, and neither `library_sync`'s real ownership insert nor a `validation_history` insert false-positives. The other four — subquery `WHERE` ahead of the truth column, f-string-built column name, `;` inside the SET body, concatenation across a non-literal — remain blind spots by decision, now recorded here and in the module. That is defensible: each requires someone to write SQL in a shape this repo does not use, the guard's value is against the ordinary regression (a handler picking up an `UPDATE games SET status=…` again), and no scanner of this kind is complete. The residual should be read as "the guard raises the cost of a regression," not "a regression is impossible."

---

### 5 — SEV-4: The breaker's Kuma push URL is a plain `str` the redactor does not recognise

**Where.** `core/settings.py:275` adds `kuma_push_measurement_breaker: str | None = None`, following the four existing `kuma_push_*` fields. The comment at `settings.py:267` and `clients/heartbeat.py:7-9` both state the URL *is* the credential.

**Concrete failure.** Nothing in this branch logs it — `measurement.py:139-153` reads it into a local, passes it to `heartbeat.push`, and on failure logs only `str(exc)[:200]`, never the URL; `heartbeat.py:56-57` likewise logs only the exception. The residual risk is that the value is unprotected by every automated control: it is not a `SecretStr` (unlike `orchestrator_token` at `settings.py:67`), and `core/logging.py:65-77`'s `_SENSITIVE_KEY_RE` matches none of `kuma`, `push`, `breaker`, or `url` — so a future `_log.info("measurement.breaker_notify", url=url)` would emit the entire push token verbatim into the log stream, with the redaction processor silently declining to fire. This branch adds the fifth such field, which is why it is raised here.

**Fix.** Add `kuma_push|push[_-]?url|webhook` to `_SENSITIVE_KEY_RE`, or type the five `kuma_push_*` fields as `SecretStr` and unwrap at the call site. Either makes the protection structural rather than a matter of nobody having typed the wrong log line yet.

**Status: Fixed in `c50399c`.** `core/logging.py:75` adds `kuma[_-]?push|push[_-]?url|webhook` to `_SENSITIVE_KEY_RE`. Re-probed against the matcher directly: all five `kuma_push_*` field names redact, as do `push_url`, `webhook_url`, `kuma-push` and the upper-case env form `KUMA_PUSH_SWEEP`; the operational keys the new log events actually emit (`status`, `game_id`, `last_job_outcome`) are correctly *not* over-redacted. `tests/core/test_logging.py` gains an end-to-end assertion that the token bytes never reach stdout. One gap remains by design: a bare `url=` key is still unmatched, so `_log.info("…", url=settings.kuma_push_sweep)` would still leak — the fix covers the field names in use, not every alias. Typing the five fields as `SecretStr` would close that; not required.

---

### 6 — SEV-4: `measurement_transitions` grows without bound

**Where.** `0015_games_measurement_split.sql:44-54`. Every successful truth write inserts one row (`measurement.py:251-255`). Nothing anywhere prunes it — `grep -rn "measurement_transitions" src/ scripts/` returns only the migration and `measurement.py`.

**Concrete failure.** `validation_sweep_cron` is `0 3,9,15,21 * * *` (`settings.py:237`) — four sweeps a day — over ~3200 owned games, and the sweep now has no status filter, so the candidate set is the whole owned library rather than the previously-filtered subset. Ceiling ≈ 12,800 rows/day ≈ 4.7M rows/year; at roughly 150–250 bytes per row all-in (STRICT table plus the rowid PK and the partial index) that is on the order of 0.7–1.2 GB/year added to a SQLite file on the LXC, for a table whose only reader looks back 60 minutes. Real throughput will be lower — the design concedes a sweep cannot finish in its 6h budget — but the growth is monotonic and unmanaged. Query cost is not the problem: `EXPLAIN QUERY PLAN` confirms the breaker count is `SEARCH measurement_transitions USING COVERING INDEX idx_measurement_transitions_window (occurred_at>?)`, so lookups stay bounded as the table grows.

**Fix.** Delete rows older than a small multiple of the window (a day is ample; the breaker never reads past 60 minutes) — either at the top of `sweep_handler`, or as a line in whatever maintenance job later handles `validation_history` retention.

**Status: Deferred (follow-up).** Unchanged in `c50399c` by decision. The growth ceiling is now marginally lower than measured above, because commanded purges no longer insert a row — an immaterial difference. Nothing about the deferral is unsafe on the timescale of this phase; it should be picked up with `validation_history` retention rather than on its own.

---

### 7 — SEV-4: The breaker's Kuma push runs inside validate's transaction and stalls every writer

**Where.** Introduced by the fix for finding 2. `validate.py:85` calls `record_measurement(..., tx=tx)` inside `async with pool.write_transaction()`; on a trip, `measurement.py:223` awaits `_notify_breaker`, whose `heartbeat.push` uses `httpx.AsyncClient(timeout=10.0)` (`clients/heartbeat.py:29`). `Pool.write_transaction` (`pool.py:1236`) runs inside `_checkout_writer`, which holds the process-wide `self._writer_lock` for the whole checkout (`pool.py:927`, and the "held for the whole checkout" comment at `pool.py:938-940`).

**Concrete failure.** Measured, not inferred. Probe: real pool, breaker primed to 24, `heartbeat.push` replaced with a 10s sleep (an unreachable Kuma), a concurrent unrelated `pool.execute_write` started 0.2s later:

```
simulated Kuma push duration: 10.0s (heartbeat.TIMEOUT_SEC)
unrelated pool.execute_write blocked for: 9.80s
total elapsed: 10.00s
```

The stall is process-wide, not sweep-local: the orchestrator runs one uvicorn process with the job dispatcher in its lifespan, so the API's write endpoints — `POST /api/v1/games/{id}/purge`, the validate and sweep triggers — queue behind the same lock and appear to hang for up to 10 seconds.

**Bound.** ≤10s (the httpx total timeout), at most once per 60-minute window (`_breaker_notice_due`, `measurement.py:104-122`), and only on the run that trips — so only when an incident is already in progress. It cannot deadlock: `record_measurement` uses the caller's `tx` for both its read and its write (`measurement.py:68-86`), so there is no re-entrant writer checkout, and `heartbeat.push` swallows every exception, so the push can never abort the transaction it sits inside.

**Status: Accepted (controller ruling 2026-09-07).** The alternative — announcing a halt the database has not yet committed to — is worse, and the exposure is a ≤10s hourly blip during an active incident. Recorded so it is a known quantity rather than a surprise. If it is ever worth removing, the cheapest hardening is to hoist the notify out of the transaction (raise `CircuitBreakerTripped` from inside, catch it in `validate_one_game` after the `async with` closes, push, then re-raise), or simply to give the breaker push a shorter timeout than the shared 10s.

## Non-findings (explicitly checked, clean)

- **No SQL injection anywhere in the diff.** Every statement is a module-level constant or an inline literal with `?` placeholders; no value is interpolated into SQL. The one f-string that reaches a query, `measurement.py:210` `(f"-{window} minutes",)`, is a **bound parameter** to `datetime('now', ?)`, not concatenated text, and `window` is `measurement_breaker_window_minutes: int = Field(default=60, ge=1)` (`settings.py:261`). Probed pydantic directly: `"1 minutes'); DROP TABLE games;--"` → `int_parsing` rejected, `abc` → `int_parsing`, `1.5` → `int_parsing`, `0` and `-5` → `greater_than_equal`. A malformed modifier would in any case make `datetime()` return NULL (probed: `datetime('now','bogus')` → `None`), which is unreachable given the validator.
- **The migration's repair predicate is exact.** Applied 0015 to a DB seeded with ten hand-built edge rows. Reset: in-window `validation_failed` (03:10:00), in-window `failed` (03:47:59, the inclusive boundary), and an in-window row with fractional seconds (`03:10:00.123`). Untouched: in-window `up_to_date` (03:30:09 — the row the design specifically warns about), `validation_failed` at 03:48:00 and 02:59:59 (both boundaries), the June-2026 `not_downloaded` rows, a `blocked` row, and a row with `last_validated_at IS NULL`. Reset rows correctly end with `status_measured_at = NULL` while keeping their attempt timestamp, so they sort by real age. The one gap is theoretical: a `T`-separated timestamp (`2026-09-01T03:10:00`) sorts above `'2026-09-01 03:47:59'` and escapes the repair — but every writer of `last_validated_at` in this codebase uses SQLite `CURRENT_TIMESTAMP`, which is space-separated, so no such row can exist.
- **No ReDoS on the build guard.** The lazy repetition in `CACHE_TRUTH_WRITE` consumes one character per iteration with no nested quantifier, so it is linear per start position: a 6400-character no-tail payload matched in 0.22 ms and scaling was exactly linear (200→0.02 ms, 6400→0.22 ms). The quadratic case (many `UPDATE games SET` anchors in one quote-free span) does exist — 800 anchors over 53,600 characters took 356 ms, 4× per doubling — but reaching it requires committing thousands of quote-free anchors into `src/orchestrator/**/*.py`, i.e. an actor who already has commit access. The real scan over the repo is **43.6 ms for 109 files**.
- **No secret reaches a log line or an exception message.** `measurement.py:139-153` never logs `url`; the breaker's Kuma message is `f"{count} games lost cache state within {window} minutes"` — two integers. `heartbeat.push` builds the request as `client.get(url.strip(), params={...})`, so the token stays in the URL and out of the message. `_notify_breaker`'s `except` logs `str(exc)[:_ERROR_TRUNCATE]`; httpx connection/timeout exceptions carry host-level text, not the push path, and `client.get` does not `raise_for_status`, so no `HTTPStatusError` (which would embed the URL) can be raised. Residual structural risk recorded as finding 5.
- **Every new log event carries only ids, statuses, counts and truncated reasons.** `measurement.breaker_tripped` / `breaker_refused` / `recorded` / `attempt_only` (`measurement.py:195,215-234,256-263`) emit `game_id`, `prior`, `new_status`, `outcome`, `downward_in_window`, `threshold` — all internal enums and integers. `job_outcome.recorded` logs `text`, already cut to `_ERROR_TRUNCATE` (200) at `measurement.py:287`. The sweep's `attempt_record_failed`, `breaker_tripped` and `aborted` (`sweep.py:107-117,138-144,186-200`) truncate at 200. The only remote-controlled strings that reach persistence are SteamPrefill's output tail (`prefill.py:372`, `raw[-150:]` then `[:200]`) and the Epic chunk tally (`prefill.py:205-210`) — both pre-existing, both double-truncated, both routed to `last_job_outcome` where nothing acts on them.
- **No authorization or input surface changed.** `git diff --name-only fd52ca2..180f2b8` touches exactly one file under `api/` — `api/main.py`, and only a comment inside `_lifespan` (the `reap_orphaned_game_status` call is unchanged). Nothing under `cli/`. No new route, no new request model, no new query parameter. `grep -rn "record_measurement|CircuitBreakerTripped"` over `src/` shows callers only in `jobs/handlers/{validate,sweep,purge}.py` — all job-dispatcher code — so `CircuitBreakerTripped` can never surface in an HTTP response; it reaches `worker.py`'s handler and marks the job failed.
- **The breaker's fail-safe direction is correct, and recovery needs no operator action.** A degraded or hostile agent returning plausible-but-false `{total, cached}` (the 2026-08-31 scenario: 65 of 256 buckets readable, ~8% cached on every game) drives `_classify` to `missing`/`partial` and thus downward transitions. That is the intended trigger. Blast radius is *reduced* by the breaker from the whole library to `threshold - 1` = 24 rows before writing stops and `sweep.py:201` aborts the run. Recovery is automatic: a refused write inserts no transition row (`measurement.py:235` raises before `measurement.py:251`), so the count is frozen and decays out of the sliding 60-minute window on its own — nothing latches, nothing needs clearing. A hostile agent can re-trip on each subsequent sweep, holding measurement frozen indefinitely at a cost of ≤24 wrong statuses per sweep, which is strictly better than the 3200 it would corrupt without the breaker.
- **The module-level dedupe state leaks nothing.** `_last_breaker_notice_at` (`measurement.py:40`) holds a single `time.monotonic()` float — no request, game, or user data. `Dockerfile:88` runs one uvicorn process with no `--workers`, so it is genuinely process-wide; a multi-worker deployment would merely emit one push per worker. It suppresses the Kuma push and the ERROR line but never the refusal (`measurement.py:224-237`), so the safety behaviour is not rate-limited, only the noise — which is the point: without it an incident-scale sweep would issue ~1000 ERROR lines and ~1000 10-second-timeout GETs.
- **Breaker TOCTOU is bounded and benign.** The count read (`measurement.py:206-211`) and the write (`measurement.py:243-255`) are not atomic on the pool path, so concurrent sweep tasks can each read the same sub-threshold count. Over-run is bounded by the sweep's concurrency, `sweep_batch_size` (default 10, `settings.py:290`) — worst case 24 + 9 = 33 downward writes land instead of 24. Direction is safe (a few extra correct writes, never a missed refusal at scale). On the transaction path `_reader`/`_writer` (`measurement.py:68-86`) deliberately pin both to the caller's connection, which is required for correctness — reading through the pool inside an open write transaction would count a pre-transaction snapshot and make a batched caller untrippable.
- **Purge's transaction is otherwise intact.** `record_measurement(..., tx=tx)` and `_record_cache_emptied(..., tx)` (`purge.py:157-161` at `c50399c`) still commit or roll back together, preserving the #293 invariant; at `180f2b8` only the breaker path broke it, and `c50399c` closes that (finding 1). Note also that a purge of an already-`not_downloaded` game records `partial`, an *upward* rank move, so it cannot itself contribute to the alarm in that case — correct, and the status it asserts (`validation_failed` with `chunks_cached=0`) matches the history row written beside it.
- **Data-plane import isolation preserved.** `measurement.py` imports only `orchestrator.clients.heartbeat` and `orchestrator.core.settings`, and nothing under `src/orchestrator/agent/` imports it (`grep -rn "measurement" src/orchestrator/agent/` returns two comment references and no import). No new `orchestrator.db` / `orchestrator.jobs` edge into the agent package. `tests/agent/test_import_isolation.py` green.
- **`measurement_transitions` integrity constraints hold.** `STRICT` table; probed `PRAGMA foreign_keys=ON` → an orphan `game_id` is rejected with `FOREIGN KEY constraint failed`, and `downward=7` with `CHECK constraint failed: downward IN (0, 1)`. `pool.py:734` applies `foreign_keys=ON` to every pooled connection (confirmed in the probe's `pool.connection_opened` line), so `ON DELETE CASCADE` really fires and deleting a game cannot strand transition rows.
- **The migration checksum is correct.** `shasum -a 256 src/orchestrator/db/migrations/0015_games_measurement_split.sql` = `32b057d04693ef9657dcd8b39400260dd296ffb7f690609809e377fb53e2b3e7`, matching the `CHECKSUMS` entry exactly. Applying 0001→0015 end to end in a probe succeeded with `migrations_complete applied_count=15`.
- **Cancellation cleanup is bounded.** `sweep.py:125-131` awaits `_record_attempt` before re-raising `CancelledError`. Only tasks already holding the semaphore reach that clause — tasks blocked on `async with sem` (`sweep.py:104`) are cancelled outside the `try` and propagate immediately — so at most `sweep_batch_size` (10) writes run, serialized on the pool's single `_writer_lock`, each capped at `busy_timeout=5000ms` of SQLite lock wait. Worst case ≈ 50s of overshoot past the 6h budget before `asyncio.wait_for` returns in `worker.py:226-228`; the write itself is wrapped in `except Exception` (`sweep.py:105-117`) so a failing or closed pool cannot mask the cancellation. The `_writer_lock` acquire has no timeout of its own, but that exposure is identical to `worker.py`'s existing `record_job_outcome` + `mark_failed` writes on the same path and is not introduced here.
- **Sweep candidate selection is indexed and unfiltered as designed.** `EXPLAIN QUERY PLAN` on both `_CANDIDATE_SQL` and `_CANDIDATE_SQL_FULL` (`sweep.py:48-57`) returns `SCAN games USING INDEX idx_games_measure_attempt`, so the new ordering rides the migration's index rather than sorting. Removing the status filter is the intended fix for D2 and adds no injectable surface — the SQL is a constant with no parameters at all.
- **Job-outcome writes cannot reach cache truth.** `record_job_outcome` (`measurement.py:286-292`) writes only `last_job_outcome`, `last_job_outcome_at` and the legacy `last_error` mirror; it has no code path to `status`. The four call sites (`worker.py:248`, `prefill.py:128,210,326,373`) and the boot reaper (`reaper.py:75-78`) all match. The reaper's `WHERE status='downloading'` is now non-clearing — those legacy rows stay `downloading` until a measurement corrects them, so each restart re-stamps the same rows — but the sweep's unfiltered candidate SQL now reaches them, and the only cost is an overwritten `last_job_outcome` on rows that carry no other useful outcome.
- **`ORCH_MEASUREMENT_BREAKER_*` and `ORCH_KUMA_PUSH_MEASUREMENT_BREAKER` are absent from operator documentation** (`grep -rln MEASUREMENT_BREAKER docs` hits only the implementation plan). Not a security defect — recorded here so Build Loop 2.5 picks it up.

## Re-verification 2026-09-07 (`180f2b8` → `c50399c`)

Remediation commit `c50399c` "fix(jobs): a purge is never refused by the breaker; validate's record is atomic" — 10 files, +312/−35. Read in full, then each claim re-probed rather than taken from the implementer's report.

**Finding 1 (SEV-2) — closed.** Re-ran the original exploit probe unmodified (real `Pool` over a migrated temp DB, real `purge_handler`, stub agent recording the delete, breaker primed to 24 with real downward measurements):

```
pre-existing downward transitions in window: 24  (threshold=25)
measurement.commanded          game_id=1 new_status=validation_failed prior=up_to_date
game.purged                    files_deleted=337 total_bytes_freed=12345
purge_handler returned normally
agent purge calls (files actually deleted): [730]
DB after:  status='validation_failed'  status_measured_at='2026-09-07 22:18:17'
           newest validation_history: 0/337 cached  (vh rows=2)
```

Every limb of the exploit is gone: no exception, status flipped, `status_measured_at` set (so the row is Epic-prefill-eligible again — the reversibility invariant holds), and the compensating history row present. `grep -rn commanded src/` confirms `commanded=True` is hardcoded at exactly one call site (`purge.py:160`) and reachable from no request-controlled path.

**Finding 2 (SEV-3a) — closed.** Re-ran the validate probe (`validate_game` stubbed to `partial 17/337`, breaker primed to 24): `raised: 25 games lost cache state within 60 minutes` → `games.status = 'up_to_date'`, `newest validation_history = NO ROW (rolled back)`. Previously the row survived. The atomicity is real, not just asserted by the new test.

**Finding 4 (SEV-4) — two of six closed.** Re-probed all seven shapes through the guard's own `writes_cache_truth()`: `INSERT INTO games (…status…)` and `INSERT OR REPLACE INTO games (id, status)` now `caught=True`, and `INSERT OR IGNORE INTO games (id, status_measured_at)` too. The four documented blind spots still return `caught=False`. No false positives: `library_sync`'s real ownership insert and a `validation_history` insert both correctly unmatched. Source scan over `src/orchestrator`: **109 files, 43.6 ms, offenders=[]** — the guard is green and still names `jobs/measurement.py` as the sole permitted writer.

**Finding 5 (SEV-4) — closed.** Probed `_SENSITIVE_KEY_RE` directly: `kuma_push_measurement_breaker`, `kuma_push_sweep`, `kuma_push_library_sync`, `kuma_push_scheduled_prefill`, `kuma_push_fetch_manifests`, `push_url`, `webhook_url`, `kuma-push`, `KUMA_PUSH_SWEEP` → all `True`; `status`, `game_id`, `last_job_outcome`, `breaker` → correctly `False`. All five pre-existing fields are covered, not only the new one.

**Finding 7 (SEV-4, new) — measured and accepted.** See the finding above; the 9.80s writer stall is a measurement from a probe, not an estimate.

**Note on the no-transition-row ruling (finding 1).** I accept it, and on reflection prefer it to what I proposed. My suggestion — count the command but never refuse it — would have let a legitimate batch of operator purges pre-load the counter, so the next sweep would trip on its first genuine eviction: a false positive on the one alarm whose entire value is that an operator believes it. The controller's framing is the sharper one: the counter measures *unexplained* loss, and a purge is explained by construction. The residual is that `measurement_transitions` is no longer a complete log of status changes, so a runaway purge loop is invisible to the mass-loss alarm — adequately covered by the `measurement.commanded` log line, the purge's own `validation_history` row written in the same transaction, and the jobs table, and bounded in the first place by purge being an authenticated, per-game, dedup-guarded endpoint. Not a new finding. One tidy-up worth making at some point: `measurement.py:165` still says "Every truth write is also logged to `measurement_transitions`" a few lines above the paragraph that documents the exception — the docstring now contradicts itself.

**Nothing of SEV-1 or SEV-2 grade appeared in the remediation diff.** The change is small and additive; `commanded` defaults to `False` so no existing caller's behaviour moves; the `error` path ignores `commanded` entirely (verified by probe and by `test_commanded_does_not_change_the_error_path`); and no new re-entrant lock acquisition is introduced, because `record_measurement` uses the caller's `tx` for both its read and its write.

## Decision

**Cleared to advance, with SEV-3/SEV-4 follow-ups.** The SEV-2 is genuinely closed — verified by re-running the original exploit probe end to end, not by reading the test names — and so is the SEV-3 history-row divergence, the SEV-4 redactor gap, and two of the six guard evasions. Nothing of SEV-1/SEV-2 grade appeared in the remediation.

Carried forward, none of them gating:

- **Finding 3 (SEV-3, Accepted by ruling).** The safety of the Epic download predicate on first deploy now rests on the rollout order (measure at step 3, re-enable at step 4) and on `ORCH_SCHEDULED_PREFILL_ENABLED` remaining false until then. Put that dependency in the deploy runbook so it survives the next person to run `deploy-orchestrator-lxc.sh`.
- **Finding 4 (SEV-4, four blind spots Deferred).** Documented, not closed. The guard raises the cost of a regression; it does not make one impossible.
- **Finding 6 (SEV-4, Deferred).** `measurement_transitions` retention, to be picked up with `validation_history` retention.
- **Finding 7 (SEV-4, Accepted by ruling).** ≤10s process-wide write stall, at most hourly, only during an active breaker incident.

## Sign-off

- Head commit audited: `c50399c` (merge-base `fd52ca2`, 14 commits, 39 files, +3481/−284); findings raised against `180f2b8`, remediation `c50399c` re-verified 2026-09-07
- `ruff check --select S src/orchestrator tests` — **All checks passed** (re-run at `c50399c`)
- `mypy --strict src/` — **Success: no issues found in 109 source files** (re-run at `c50399c`)
- `semgrep --config=p/owasp-top-ten --config=.semgrep/` over the 35 changed Python files — **159 rules, 0 findings, 0 errors** (re-run at `c50399c`)
- Full suite per the implementer: **1834 passed**. Independently re-run here by directory over every affected area — `tests/jobs tests/core/test_logging.py tests/test_measurement_writer_guard.py tests/db/test_migration_0015_measurement_split.py tests/scheduler tests/agent/test_import_isolation.py` — **329 passed**. (An earlier hand-picked file list produced 6 `fixture 'pool' not found` setup errors in `tests/jobs/handlers/`; that directory has no conftest of its own and inherits the `pool` re-export from `tests/jobs/conftest.py`, which pytest resolves when the paths are collected as directories but not in that particular mixed invocation. The same files pass alone and by directory — a collection artifact of the ad-hoc invocation, not a defect.)
- Migration checksum verified: `0015` → `32b057d04693ef9657dcd8b39400260dd296ffb7f690609809e377fb53e2b3e7`
- Agent import isolation preserved; no new dependency; one migration (0015)
- Findings: 0 SEV-1, 1 SEV-2 (**fixed**), 2 SEV-3 (1 fixed, 1 accepted), 4 SEV-4 (1 fixed, 1 partly fixed, 1 accepted, 1 deferred)
