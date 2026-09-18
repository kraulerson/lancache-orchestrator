# UAT Session 16 — Exploratory Testing (Malicious User)

Date: 2026-09-15
Branch at test time: `fix/313-heartbeat-delivery-gates-stamp` (confirmed via `git branch --show-current`)
Targets: sweep pass marker (#311/#322), breaker heartbeat delivery (#313), purge delete-then-validate (#321)

All testing was local: temp SQLite DBs under `/private/tmp/.../scratchpad/`, no production hosts touched, no files under `src/` or `tests/` modified, nothing committed. Scratch scripts have been deleted after this report was written.

---

## Confirmed findings

### Finding 1 — SEV-3: `get_settings()` raising inside the breaker-check path masks `CircuitBreakerTripped`, defeating the sweep's "stop everything" guarantee

**File:** `src/orchestrator/jobs/measurement.py`, `record_measurement()` (the `if downward and not commanded:` block, ~line 253) and `_notify_breaker()` (~line 162).

Both call `get_settings()` **outside any try/except**. Everything else on this path is deliberately exception-safe — `heartbeat.push()` never raises, and `_notify_breaker()` wraps its own call to `heartbeat.push()` in a try/except specifically because "Monitoring must never break the thing it monitors." But the `get_settings()` calls themselves are unguarded in *two* places on the same path.

If `get_settings()` raises at that moment, `record_measurement()` propagates the raw exception instead of `CircuitBreakerTripped`. `sweep.py`'s `_one()` distinguishes the two:
```python
except CircuitBreakerTripped as e:   # abort the WHOLE sweep
    ...
except Exception as e:               # isolate — one bad game never aborts the sweep
    await _record_attempt(game_id)
    ...
```
So a masked breaker trip is treated as an ordinary per-game hiccup and the sweep **keeps going** instead of halting writes — exactly the failure mode the breaker exists to prevent.

**Script (Part 1 — direct call):**
```python
def _boom():
    raise _SettingsExplodes("settings reload mid-flight")
measurement_mod.get_settings = _boom
await record_measurement(pool, game_id, "missing")  # up_to_date -> not_downloaded: downward
```
**Observed output:**
```
CONFIRMED: record_measurement leaked the raw get_settings() exception (_SettingsExplodes: settings reload mid-flight) INSTEAD OF CircuitBreakerTripped
```

**Script (Part 2 — through `sweep_handler` with two candidate games, batch size forced to 1 for determinism):**
Game 1's `validate_one_game` stand-in triggers the `get_settings()` explosion during its (downward) measurement; game 2 is a normal candidate.
**Observed output:**
```
call_order = ['validate:1', 'validate:2']
CONFIRMED: sweep_handler processed BOTH games -- the get_settings() exception from game 1's breaker check did NOT abort the sweep the way a real CircuitBreakerTripped would have.
summary: JobSummary(ok=True, msg='pass 1 complete: 2 games, 0.0 GiB')
```
**Control (real `CircuitBreakerTripped` from game 1, same setup):**
```
control call_order = ['validate:1']
As designed: a REAL CircuitBreakerTripped aborts the sweep -- only game 1 was attempted, game 2 was never touched, and the job fails loud.
```
The contrast is exact: same DB, same two candidates, only the exception type differs, and it changes the sweep from "abort, 1 game touched" to "complete, both games touched, job reports `ok=True`."

**Why it matters:** if this ever fires for real, an operator loses the one signal that's supposed to say "writing has halted, an incident is in progress" — the sweep just quietly keeps measuring (and, worse, keeps *writing* downward transitions for every subsequent game in the batch, since the veto never engages) while reporting success.

**Reachability — be honest about this:** `get_settings()` is `@lru_cache`; once it succeeds at boot it is not re-evaluated. `reload_settings()` (which clears that cache) is **never called from production code** — confirmed by `grep -rn "reload_settings\|cache_clear" src/orchestrator/` returning zero hits outside `settings.py` itself and test fixtures. So **this is not reachable via any input available today** — it requires monkeypatching `get_settings` directly, which only a test (or a future SIGHUP-reload feature the docstring calls out as "future work") can do. Rated SEV-3 rather than SEV-1/2 for that reason: it's a real, confirmed logic gap and an inconsistency with the module's own stated invariant ("monitoring must never break the thing it monitors" — but here, *reading config* can), but it is currently latent, not exploitable in the deployed system.

---

### Finding 2 — SEV-2: unvalidated agent JSON response can crash `purge_handler` after the delete already happened, leaving a false-green status with zero audit trail

**Files:** `src/orchestrator/clients/agent_client.py` (`steam_purge`/`epic_purge`: `result: dict[str, Any] = resp.json()` — no shape/type validation at all) and `src/orchestrator/jobs/handlers/purge.py` (`purge_handler`, the three `int(result.get(...))` lines, which run **before** the post-delete `validate_one_game(..., commanded=True)` call).

The purge design explicitly does not trust the agent's own delete counts for cache truth ("cache truth comes from a real validation of the disk afterwards, not from the agent's own report") — but that safety property only holds if `validate_one_game()` actually runs. If the `int()` casts blow up first, it never does.

**Script:** fake agent whose `steam_purge()` returns `{"deleted": None, "failed": 0, "bytes_freed": 0}` — simulating a real delete on the agent side with a malformed/drifted response shape (schema drift, agent bug, mangled proxy response).

**Observed output:**
```
before purge: status='up_to_date'
purge_handler raised TypeError: int() argument must be a string, a bytes-like object or a real number, not 'NoneType'  (job would be marked failed)
after purge:  status='up_to_date' (unchanged: True)
validation_history rows for this game: 0
measurement_transitions rows for this game: 0

CONFIRMED: the agent's delete call ran (files gone, per the fake agent), purge_handler crashed on the malformed response BEFORE calling validate_one_game(), so games.status is still 'up_to_date' and there is ZERO durable record (validation_history / measurement_transitions) that a delete was ever attempted. The job fails loud (good), but the game shows a false-green cache badge until the next sweep re-measures it -- which, per the project's own notes, can be ~12.5h away.
```

A second script confirmed the same crash-before-measurement pattern for a non-numeric string value (`"deleted": "12.5"` → `ValueError: invalid literal for int() with base 10: '12.5'`), so it is not specific to `null` — any value `int()` can't coerce reproduces it.

**Why it matters:** the job does fail loud (no silent success), which is good — but the *consequence* of that failure is a state the rest of the system (Game_shelf's badge, the sweep's candidate ordering) cannot distinguish from "never purged." Per `CLAUDE.md`'s own history, this project has already hit two live incidents where the agent returned an unexpected/degenerate response shape (the uid-1000 EACCES case returning `{"deleted": 0, "failed": N}`) — so "the agent returns something purge_handler doesn't expect" is not a hypothetical for this codebase, it has happened. This particular variant (a field that's present but not int-coercible) hasn't been observed live, but nothing here validates against it.

**Reachability:** requires the data-plane agent (a separately deployed, evolving service) to return a response shape `purge_handler` doesn't expect. Not attacker-controlled from outside the trust boundary, but realistically reachable by an agent-side bug or version-skew between control-plane and agent — i.e. a real operational input class for this specific system, not a synthetic-only construction. That's why this is SEV-2 rather than SEV-3/4.

**Suggested direction (not implemented — architecture decision, not mine to make):** validate/clamp the agent response shape in `agent_client.steam_purge`/`epic_purge`, or wrap the `int()` conversions in `purge_handler` so a shape mismatch still reaches `validate_one_game()` and gets an honest measurement recorded.

---

## Attacks tried that did NOT break anything (robustness evidence)

### 1. True concurrent `complete_pass()` race (10-way `asyncio.gather`, same stale view)
Fired 10 concurrent `complete_pass(pool, current)` calls all holding the identical stale `SweepPass(number=1, ...)` view against a real `Pool` (not sequential, unlike the existing `test_complete_pass_is_idempotent_against_a_stale_view`).

**Observed output:**
```
initial pass: number=1 started_at='2026-09-15 19:44:08'
  racer 0..9: returned number=2 started_at='2026-09-15 19:44:08'
final pass: number=2 started_at='2026-09-15 19:44:08'
PASS: pass advanced exactly once (1 -> 2) despite 10 concurrent racers
PASS: every racer's return value agrees with final DB state
```
The `WHERE pass_number = current.number` guard combined with the pool's single-writer serialization holds under real concurrency, not just sequential simulation. `pass_started_at` was never double-restamped.

### 2. `sweep_pass` CHECK constraints and idempotent re-migration
- `UPDATE sweep_pass SET pass_number = 0` → rejected (`check constraint failed`).
- `UPDATE sweep_pass SET pass_number = -5` → rejected (`check constraint failed`).
- `INSERT INTO sweep_pass (id=2, ...)` (second row) → rejected (`check constraint failed`).
- Re-running `migrate.run_migrations()` against an already-migrated DB file → clean no-op (checksum-verified idempotency held).

### 3. Deleting the only `sweep_pass` row
`DELETE FROM sweep_pass WHERE id=1` then `read_pass(pool)` → raised `RuntimeError: sweep_pass row is missing — migration 0017 did not run`, exactly as documented in the source comment. `sweep_handler()` does not catch this — it propagates all the way out, which fails the job loudly rather than silently no-op'ing or corrupting state:
```
CONFIRMED fail-loud: sweep_handler propagated RuntimeError: sweep_pass row is missing — migration 0017 did not run
(the job worker will mark this job 'failed' -- an operator sees it, rather than the sweep silently no-op'ing forever)
```

### 4. Settings boot-guard boundary conditions (`Settings` validator, `_reject_sweep_margin_that_swallows_the_budget`)
| case | result |
|---|---|
| margin == budget (100 == 100) | REJECTED |
| margin = budget − 1e-9 | ACCEPTED (correct: strictly `>=` is the reject condition, and the code comments call this "wrong in the safe direction" on purpose) |
| margin = budget + 1e-9 | REJECTED |
| margin = −1 | REJECTED (`ge=0.0` on the field) |
| budget = −1 | REJECTED (`ge=0.0` on the field) |
| budget = 0, margin = 1e12 | ACCEPTED (documented: `job_max_runtime_sec=0` disables the deadline entirely, so nothing to validate against) |
| budget = 0.0001, margin = 0 | ACCEPTED |
| budget = 0.0001, margin = 0.0001 (equal, both tiny) | REJECTED — the boundary check is scale-invariant, not just checked against "round" numbers |
| budget = nan, or margin = nan | REJECTED (pydantic's float validation rejects NaN by default) |

One **informational, not exploitable** observation from this pass: `job_max_runtime_sec = inf` is **ACCEPTED** (`ge=0.0` does not exclude `+inf`, and `inf > 0` is `True` so the boot guard's `>=` comparison against a finite margin also passes). In `sweep.py` this produces `deadline = monotonic() + inf - margin = inf`, which is behaviorally identical to the documented "no deadline" path (`budget = 0`) — so it doesn't appear to break anything, it's just an undocumented second way to reach the same "no cooperative deadline" state, reachable only if an operator sets `ORCH_JOB_MAX_RUNTIME_SEC=inf` (Python's `float()` does parse the literal string `"inf"`). Not raised as a finding because I could not construct a scenario where it behaves differently from the documented `budget=0` disable path — flagging only because it's an unvalidated edge the `ge=0.0` constraint doesn't cover.

### 5. Circuit breaker push-storm / retry backoff
Already has dedicated coverage in `tests/jobs/test_measurement_circuit_breaker.py` (`test_a_failed_push_backs_off_instead_of_retrying_every_game`, `test_a_delivered_push_still_suppresses_for_the_window`, `test_an_undelivered_push_does_not_silence_the_incident`). I read through the `_breaker_notice_due()` / `_stamp_breaker_notice()` logic and traced it against those tests rather than re-deriving a duplicate script — did not find a gap beyond Finding 1 above (which is specifically about `get_settings()`, not about the push/dedupe logic itself, which is exception-safe).

### 6. Purge with negative / enormous / missing-key / boolean agent-response fields
Ran `purge_handler` against `{"deleted": -999999, "failed": -1, "bytes_freed": -5}`, `{"deleted": 10**18, ...}`, `{}` (empty dict, relies on `.get(..., 0)` defaults), and `{"deleted": True, "failed": False, "bytes_freed": 0}`. In every one of these cases the `int()` casts in `purge_handler` completed without raising (Python's arbitrary-precision ints and `bool`-as-`int` coercion absorbed all of them cleanly) — the crash in Finding 2 is specific to values `int()` genuinely cannot parse (`None`, non-numeric strings), not to sign or magnitude. (Downstream of that, each run hit an `AttributeError` from my minimal fake agent missing a `steam_validate` method needed for the post-delete measurement — a harness limitation, not an app bug; I did not chase full end-to-end behavior for negative/huge counts past the parsing stage.)

### 7. Pass starvation via a permanently-unstamped game
Attempted to construct a DB state where a game could never leave the candidate set (i.e., `last_measure_attempt_at` never gets stamped, forever). Traced the only path that could cause this: `_record_attempt()`'s own `record_measurement(pool, game_id, "error")` write itself raising and being swallowed (logged as `sweep.attempt_record_failed`, per the code's explicit "must not mask the failure... during shutdown" design). This requires the stamp write to fail *specifically and repeatedly for one game* while every other write to the same table succeeds — under SQLite's single-writer model I could not construct a reproducible scenario for this (a DB-wide fault fails every write, not one game's; a per-row fault like a missing game row means the game no longer appears as a candidate at all). Not able to break this; treating as unconfirmed rather than robust, since I did not exhaustively rule out every fault-injection angle (e.g. a corrupted single row) in the time available.

---

## Summary

| # | Severity | Finding | Reachable from real inputs? |
|---|---|---|---|
| 1 | SEV-3 | `get_settings()` exceptions inside `record_measurement`'s breaker path mask `CircuitBreakerTripped`, letting the sweep continue instead of aborting | No — `reload_settings()` is never wired to anything live today; confirmed via monkeypatching only |
| 2 | SEV-2 | Unvalidated agent JSON response can crash `purge_handler` *after* the delete but *before* the corrective measurement, leaving a false-green status with no audit row | Yes in principle — the agent is a real, separately-evolving service with a documented history of unexpected response shapes on this exact endpoint family; not externally attacker-controlled, but a realistic operational fault |

Everything else attempted (concurrent pass-marker races, CHECK-constraint bypass attempts, migration replay, missing-marker fail-loud behavior, settings boundary math, breaker push-storm suppression, and malformed purge counts short of non-coercible values) held up under execution.
