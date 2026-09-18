# UAT Session 16 — Automated Suite Audit

**Date:** 2026-09-15
**Branch:** `fix/313-heartbeat-delivery-gates-stamp` (verified via `git branch --show-current`; not switched, not committed to)
**Scope:** #311/#322 sweep pass marker (deployed), #313 breaker heartbeat delivery (PR #327, not deployed), #321 purge delete-then-validate (deployed, never live-exercised)
**Persona:** QA Test Engineer — hunting for coverage gaps and assertions that would pass under a subtly wrong implementation.

---

## Summary

| Check | Result |
|---|---|
| `pytest -q` | **1912 passed, 3 deselected, 1 warning**, 66.71s |
| `ruff check src tests` | **All checks passed** |
| `mypy src` | **Success: no issues found in 110 source files** |
| `test_measurement_writer_guard.py` | **Passes** (2 tests) — guard is unchanged; see Finding 1 |
| Dependency-bump warning check (task 5) | **Could not be performed — see Finding 2** |

Both commands were run with `PATH="$PWD/.venv/bin:$PATH"` as mandated.

---

## Findings

### Finding 1 — SEV-2 — Test venv is stale relative to `requirements.txt`/`requirements-dev.txt`; task 5 (dependency-bump regression check) is unanswerable as run

**Evidence:**
```
$ grep -E "^starlette|^uvicorn|^hypothesis" requirements.txt requirements-dev.txt
requirements.txt:starlette==1.6.0
requirements.txt:uvicorn[standard]==0.52.4
requirements-dev.txt:hypothesis==6.168.0
requirements-dev.txt:starlette==1.6.0
requirements-dev.txt:uvicorn[standard]==0.52.4

$ PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pip list | grep -Ei "starlette|uvicorn|hypothesis|httpx|fastapi"
fastapi     0.141.1
httpx       0.28.1
hypothesis  6.165.2   # requirements-dev.txt pins 6.168.0
starlette   1.5.0     # requirements.txt pins 1.6.0
uvicorn     0.52.1    # requirements.txt pins 0.52.4
```

**Why it matters:** the three merged dependabot commits on this branch (`9d1b126` starlette 1.5.0→1.6.0, `1e2e73b` uvicorn 0.52.3→0.52.4, `d2cc20b` hypothesis 6.165.9→6.168.0 — all dated 2026-09-15, today) updated the lockfiles but `.venv` was never reinstalled from them. The "1912 passed, 1 warning" run above therefore exercised starlette 1.5.0 / uvicorn 0.52.1 / hypothesis 6.165.2 — none of the pinned, merged versions. **Task 5 of this audit cannot be answered from this environment**: no deprecation warning or behavior change from the actual bumped packages can be observed until the venv is reinstalled (`pip install -r requirements-dev.txt`), which this audit did not do (out of scope — read-only, no environment mutation authorized).

**Concrete failure scenario:** starlette 1.6.0 or uvicorn 0.52.4 could introduce a behavior change (deprecation warning escalated to error, a changed default, a removed API) that breaks CI or production, and this local "the tests pass" signal would say nothing about it — CI's fresh install is the only place that would ever see it. Given `uvicorn` is what runs the production LXC service, this is not cosmetic.

**Recommendation:** reinstall `.venv` from `requirements-dev.txt` and rerun the full suite plus `-W error::DeprecationWarning` before this UAT session's gate passes, or explicitly confirm CI (which presumably installs fresh) already ran green on these three commits and treat that as the check instead.

---

### Finding 2 — SEV-3 — `_BREAKER_RETRY_SEC` is exercised but its *rate-bounding* property is never proven

Task asked: "does anything prove `_BREAKER_RETRY_SEC` bounds the retry rate rather than merely existing?" — **No.**

`tests/jobs/test_measurement_circuit_breaker.py:513-536` (`test_a_failed_push_backs_off_instead_of_retrying_every_game`) seeds 40 games and refuses all of them in a tight `for` loop with no manipulation of the clock. Real wall-clock elapsed time during that loop is on the order of milliseconds — far under the real `_BREAKER_RETRY_SEC = 60.0`. The assertion `len(attempts) == 1` would pass identically if:
- `_BREAKER_RETRY_SEC` were `3600.0` instead of `60.0`, or
- the retry-backoff feature did not exist at all and undelivered pushes used the full 60-minute window (the pre-#313 behavior this fix replaces) — the test cannot distinguish "retries after 60s" from "retries after 60min" from "never retries in this test's lifetime."

`test_an_undelivered_push_does_not_silence_the_incident` (line 460-488) is the complementary test, but it monkeypatches `measurement._BREAKER_RETRY_SEC = 0.0`, which sidesteps the actual constant's value entirely — it proves a retry *can* happen when the backoff is zero, not that the real 60-second value governs *when* it happens.

No test in this file drives `time.monotonic()` (e.g. via `monkeypatch.setattr(measurement.time, "monotonic", ...)`, the same pattern `test_sweep_pass_marker.py` uses for the sweep deadline) to assert: refused at t=0 → still suppressed at t=59s → retried at t=60s. That would be the actual proof the constant does what its name claims.

**Concrete failure scenario:** someone accidentally sets `_BREAKER_RETRY_SEC = 6000.0` (an extra zero) or removes the distinct-from-`window_minutes` behavior and hardcodes the full window for both paths — every test in the file still passes.

---

### Finding 3 — SEV-3 — `_breaker_notice_due()` / `_stamp_breaker_notice()` split is a TOCTOU race under real sweep concurrency, and no test exercises concurrent trips

`measurement.py:112-128` documents the check (`_breaker_notice_due`) and the stamp (`_stamp_breaker_notice`) as deliberately separate steps, with an `await _notify_breaker(...)` network call in between (`measurement.py:267-279`). `_next_breaker_notice_at` is a bare module-level global with no lock around it.

`sweep_handler` runs games through `asyncio.Semaphore(settings.sweep_batch_size)` with `sweep_batch_size` defaulting to 2 (`settings.py:300`) — i.e., production runs *concurrent* `_one()` tasks, each independently calling `validate_one_game` → `record_measurement`. If two games trip the breaker in the same tick (plausible: a mass-eviction event would flip many games downward at once), both coroutines can pass `_breaker_notice_due()` (neither has stamped yet), both `await _notify_breaker(...)` concurrently, and both stamp — producing two Kuma pushes instead of one, defeating exactly the "notify once" contract `test_repeated_refusals_notify_once` (line 199) exists to protect.

Every test that exercises the dedupe/backoff logic — `test_repeated_refusals_notify_once`, `test_repeated_refusals_log_error_once_then_info`, `test_an_undelivered_push_does_not_silence_the_incident`, `test_a_delivered_push_still_suppresses_for_the_window`, `test_a_failed_push_backs_off_instead_of_retrying_every_game` — drives `record_measurement` through `_refuse_everything`, a plain sequential `for` loop (`test_measurement_circuit_breaker.py:186-196`). None uses `asyncio.gather` to call `record_measurement` concurrently, so the race the `sweep_batch_size=2` default actually creates in production is untested.

**Concrete failure scenario:** during a real mass-eviction incident with the default batch size, the operator gets 2+ Kuma DOWN pushes for one incident (noisy but survivable) — or, if `_notify_breaker` itself isn't reentrant-safe in some future change, worse. Not a regression from this branch (the race predates #313's split), but #313's split of check-then-act makes the window strictly wider than the old check-and-stamp-together version, and nothing here caught that the fix reintroduces/widens a race.

---

### Finding 4 — SEV-3 — the deadline-vs-validate ordering proof is only exercised at `sweep_batch_size=1`

Task asked whether any test proves the deadline check happens *before* a game is validated. `test_the_deadline_stops_the_run_without_completing_the_pass` (`test_sweep_pass_marker.py:271-301`) does prove this correctly for the serialized (`sweep_batch_size=1`, forced at `_settings()` line 82) case: the 3rd game's `fake_validate_one` is provably never called once the fake clock crosses the deadline.

But **every** test in `test_sweep_pass_marker.py` forces `sweep_batch_size: 1` "for deterministic ordering." The real deadline check (`sweep.py:189-192`) runs *inside* the semaphore-guarded section, so under the production default `sweep_batch_size=2` (or higher), up to N games can be mid-flight when the deadline is crossed — the ordering guarantee ("stops starting new games," not "stops all games") is real but is never demonstrated with actual concurrency (e.g., two games racing where one starts just before the deadline and one just after, with real interleaving via `asyncio.gather`, not a serialized loop). The current test proves the single-worker case is correct; it says nothing about whether the `deadline_hit` flag and the `lock`-guarded counters behave correctly when multiple `_one()` coroutines are genuinely concurrent.

---

### Finding 5 — SEV-4 — `full` mode's inclusion of unowned games is asserted in a docstring/comment, never in a test

`sweep.py:70-73` states in a comment that `full` mode "additionally includes unowned games" via `_CANDIDATE_SQL_FULL`, which indeed has no `WHERE owned = 1` clause (contrast with the gated `_CANDIDATE_SQL` at line 64-68, which does).

`test_full_mode_ignores_the_pass_marker` (`test_sweep_pass_marker.py:183-203`) is the only test that exercises `full: true`, and it seeds only an owned game (`_seed`, `test_sweep_pass_marker.py:62-70`, hardcodes `owned=1`). No test in the new suite (or `test_sweep_ordering.py`) seeds an unowned game and asserts it appears in `full` mode's candidate set but not in the gated set.

**Concrete failure scenario:** someone "fixes" `_CANDIDATE_SQL_FULL` by adding `WHERE owned = 1` (arguably looks more consistent with the gated query at a glance) — the Game_shelf "validate everything" manual override silently stops covering unowned games, and no test fails.

---

### Finding 6 — SEV-4 — three purge branches remain untested (pre-existing gap, not introduced by #321's rework, but never closed)

Read line-by-line against `purge.py`:

- `purge.py:93-94` — Epic manifest present but `cdn_base` falsy (`raise ValueError(f"epic game {game_id} manifest has no cdn_base (re-prefill first)")`). No test seeds a manifest row with `cdn_base=None`/`""`. `tests/jobs/test_purge_handler.py:170` (`test_epic_no_manifest_raises_and_leaves_status`) covers the *no manifest at all* case only.
- `purge.py:96-98` — Epic `app_id` non-numeric silently falls back to `app_id_int = 0` rather than raising (asymmetric with the Steam path two lines below, which raises `ValueError`). No test exercises a non-numeric Epic `app_id`, so this silent-zero fallback — which looks like it could purge the wrong (or a nonexistent) Epic app — is unverified in both directions.
- `purge.py:135-137` — Steam non-numeric `app_id` → `raise ValueError(f"steam app_id not numeric: {game['app_id']!r}")`. No test seeds a Steam game with a non-numeric `app_id` to hit this.

The delete-then-validate fallback-to-`partial` path that the task specifically flagged as a concern **is** well covered — `tests/jobs/test_purge_measures_the_disk.py:226-287` has both the "deleted>0 but post-purge validation errors" case (falls back to `partial`, asserts `chunks_total` carried from the pre-purge row) and the "deleted==0 and validation errors" case (status left alone). That part of #321 is a non-finding; see below.

---

## Untested Paths

| File:line | Branch | Covered? | Note |
|---|---|---|---|
| `sweep.py:117-119` | `full` payload parse — malformed/non-dict `payload` (`json.JSONDecodeError`/`TypeError`/`AttributeError` → `full=False`) | No | No test sends a malformed `payload` string to `sweep_handler`; defaults to `False` untested |
| `sweep.py:70-77` `_CANDIDATE_SQL_FULL` | `full=True` includes unowned games | No | Finding 5 |
| `sweep.py:189-192` | deadline check under real concurrency (`sweep_batch_size > 1`) | No | Finding 4 |
| `sweep.py:297` | `current is None` guard after the `full` branch | N/A — `# pragma: no cover - unreachable`, and genuinely unreachable given `read_pass` always returns or raises | Correctly excluded |
| `sweep_pass.py:66-67` | `complete_pass` no-op warning (`nxt.number == current.number`) | N/A — `# pragma: no cover - defensive`; analysis below shows this is provably unreachable given monotonic increment + `WHERE pass_number = current.number` guard, so exclusion is justified | Non-finding |
| `heartbeat.py:58-59` | whitespace-only URL returns `True` (no-op) | Yes | `test_push_with_no_url_reports_nothing_to_retry` |
| `measurement.py:112-146` | retry-rate bound of `_BREAKER_RETRY_SEC` (60s, not 0) | No | Finding 2 |
| `measurement.py:112-128` | concurrent trip race on `_next_breaker_notice_at` | No | Finding 3 |
| `purge.py:93-94` | Epic manifest with falsy `cdn_base` | No | Finding 6 |
| `purge.py:96-98` | Epic non-numeric `app_id` → silent 0 | No | Finding 6 |
| `purge.py:135-137` | Steam non-numeric `app_id` → `ValueError` | No | Finding 6 |
| `purge.py:180-189` | fallback to `partial` when `deleted>0` and post-purge validation errors | Yes | `test_a_failed_post_purge_validation_after_real_deletes_still_clears_the_badge` |
| `purge.py:180-189` | no fallback write when `deleted==0` and validation errors | Yes | `test_a_failed_post_purge_validation_that_deleted_nothing_changes_nothing` |

---

## Non-findings (checked, clean)

- **Pass marker actually gates re-measurement within a pass**: `test_a_game_already_attempted_this_pass_is_not_attempted_again` (`test_sweep_pass_marker.py:145-162`) is a genuine proof, not a tautology — it seeds one game attempted *after* the pass start (excluded) and one *before* (included), and asserts the excluded game's id never reaches `validate_one_game`. An implementation that used `<` instead of the documented `<=`, or that gated on the wrong column, would fail this test.
- **Deadline-before-validate ordering (single-worker case)**: genuinely proven, not just asserted — see Finding 4 for the concurrency caveat, but the serialized case is solid: `fake_validate_one` for game 3 is never called once the fake clock crosses the deadline (`test_the_deadline_stops_the_run_without_completing_the_pass`).
- **`complete_pass` idempotency against a stale view**: `test_complete_pass_is_idempotent_against_a_stale_view` genuinely exercises the guarded UPDATE (calls `complete_pass` twice with the same stale `SweepPass`, asserts only one advance happened) — not a tautological assertion.
- **Migration 0017**: all four tests are meaningful — table creation, seed content, the `CHECK (id=1)` single-row constraint (asserts `IntegrityError` on a second row), and the seeded timestamp ordering relative to a pre-existing `last_measure_attempt_at`. No gaps found here.
- **Heartbeat whitespace-URL handling**: explicitly tested for both the no-request-sent behavior (`test_no_url_configured_is_a_silent_no_op`, includes `"   "` in its parametrize) and the return-value contract (`test_push_with_no_url_reports_nothing_to_retry`).
- **Circuit breaker `commanded` exemption (#310/#321 interaction)**: thoroughly tested in both `test_measurement_circuit_breaker.py` (veto bypass, logged-but-not-counted, log event content, `error`-path unaffected) and re-verified at the purge-handler level (`test_purge_measures_the_disk.py:173-224`, `test_purge_handler.py:92-125`). No gaps.
- **`test_measurement_writer_guard.py`**: still passes (2/2). Confirmed via `gh issue view 312` that the regex at `test_measurement_writer_guard.py:36-53` is byte-for-byte the version issue #312 was filed against, and none of its 9 (issue text: 10) documented evasions (`REPLACE INTO`, table alias, `UPDATE OR REPLACE`, subquery `WHERE`, `;` in a string literal, double-quoted identifier, explicit `+` concatenation, f-string column interpolation, triple-quoted INSERT, schema-qualified `main.games`) are in `_MUST_MATCH`/`_MUST_NOT_MATCH`, and the regex itself is unchanged from what the issue describes. **Confirmed still open and unfixed**, consistent with its `triage:phase-3` label and the UAT-15 triage note deferring it. This is not a new finding — flagging only to confirm the state has not silently regressed further or been partially patched.
- **`ruff check` / `mypy`**: both fully clean, no suppressions or new `# type: ignore` / `# noqa` added by the three features under audit beyond the one pre-existing, documented `# noqa: N818` on `CircuitBreakerTripped`.

---

## Recommendation for the UAT gate

Do not pass this session's automated-suite gate on Finding 1 alone: reinstall `.venv` against the current lockfiles and rerun before signing off task 5. Findings 2-4 are coverage gaps worth a follow-up issue (pattern: `_BREAKER_RETRY_SEC` clock-driven test, one concurrent-trip test with `asyncio.gather`, one `sweep_batch_size>1` deadline test) but are not blocking — the underlying behavior is plausible-correct by code reading, just unproven. Findings 5-6 are cheap to close (a handful of new test cases) and worth doing before the next UAT session touches purge or full-mode sweeps.
