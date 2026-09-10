# UAT-14 — Automated Suite (QA Test Engineer)

**Date:** 2026-08-24
**Branch:** `chore/session-parking` @ `ab38fb1`
**Command:** `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q -rs --durations=30`
**Environment:** macOS 25.4.0 (darwin/arm64), Python 3.12.13, pytest 9.1.1, pytest-asyncio 1.4.0, hypothesis 6.165.2

---

## Summary

| Metric | Value |
|---|---|
| Passed | **1676** |
| Failed | **0** |
| Errors | **0** |
| Skipped | **0** |
| xfail / xpass | **0 / 0** |
| Deselected | **3** (silently, by default config) |
| Wall time | **44.18 s** (run 1) / **43.13 s** (run 2) |
| Warnings | 1 (third-party deprecation) |

Verbatim summary line, both runs:

```
1676 passed, 3 deselected, 1 warning in 44.18s
1676 passed, 3 deselected, 1 warning in 43.13s
```

**Verdict: the suite is green, and the green is partly hollow.**

Two things are true at once and both matter:

1. There are **zero failures**. Test count is up from the 1636 recorded in `CLAUDE.md` to 1676. Nothing is broken.
2. **Eleven separate production mutations — including deleting the path-safety guard from both destructive purge endpoints, and truncating the Steam manifest fetcher to a single depot — leave all 1676 tests green.** These were not inferred; each was applied to the source, re-run against the *full* suite, and reverted. See [Tests that cannot fail](#tests-that-cannot-fail).

Additionally, **the documented run command does not reliably terminate on this machine**. The first two attempts hung past 10 minutes. Root cause is in-repo and is described under [Timing and flakiness](#timing-and-flakiness). It is SEV-2 and is arguably the single most disruptive finding for day-to-day work, even though it is not a product bug.

---

## Failures

**None.** Two consecutive full runs, 1676 passed each, zero failures, zero errors.

---

## Skips and xfails

### Actual skips at runtime: zero

`-rs` and `-rx` produced no skip or xfail report in either run. There is exactly one conditional skip in the codebase and it does not fire on this platform:

- `tests/db/test_migrate.py:608` — `pytest.skip("/dev/null not available on this platform")` inside `test_uat2_v3_rejects_character_device_database_path`. Guarded by `if not Path("/dev/null").exists()`. Does not fire on macOS or Linux. **Not a finding** — correctly scoped, and the test it guards runs.

### SEV-2 — Three tests are permanently deselected by default and have almost certainly never run

`pyproject.toml`:

```toml
markers = [
    "slow: marks tests that simulate sustained workload (deselect with -m 'not slow' or run via -m slow)",
]
addopts = ["-m", "not slow"]
```

`addopts` is unconditional, so **`-m "not slow"` is applied to every invocation** — the documented `CLAUDE.md` command, a bare `pytest`, and CI alike. The three tests are:

```
tests/db/test_pool_slow.py::test_sustained_concurrent_workload
tests/db/test_pool_slow.py::test_replacement_storm_guard_under_load
tests/db/test_pool_slow.py::test_long_running_streaming_read_under_concurrent_writes
```

Why this is worse than an ordinary skip:

- A deselection is **not** reported by `-rs`. It appears only as the word `deselected` in the summary count. Nobody reading `1676 passed` sees three missing tests.
- These are the DB connection-pool endurance tests — precisely the subsystem that produced the SEV-2 pool deadlock / scheduler leak cluster in the 2026-06-02 code review (`#131`–`#134`). The regression net for that incident is switched off by default.
- Nothing in `CLAUDE.md`, CI config, or the test-gate scripts ever runs `-m slow`, so there is no compensating scheduled execution.

The marker's own docstring says "run via `-m slow`" — a documented escape hatch that is not wired to anything. The mechanism is fine; leaving it permanently in the "off" position with no counterpart job is the finding.

---

## Coverage by area

Assessments below distinguish **exercised** (a mutation to the named production line breaks a test) from **happy-path only** (the branch exists but nothing drives it). Where a mutation was actually run, the result is stated.

### 1. F18 cache purge — SEV-2 (guard wiring), otherwise strong

**Genuinely exercised.** This area has the best behavioural coverage in the change set. `tests/agent/test_steam_purge.py` and `tests/agent/test_epic_purge.py` build **real chunk files on disk** through the same `cache_key`/`cache_path` hashing production uses and assert real deletion — not mocks. Covered: idempotent re-purge, no-manifest → `{deleted: 0}` rather than error, auth rejection leaving files intact, `422` on a negative `app_id`, differing-group-id secondary depots, Epic dual-identifier candidates, malformed Epic manifest → `0` not `500`.

Handler and API: `tests/jobs/test_purge_handler.py` proves the `validation_failed` reversibility invariant and that an `AgentError` leaves status untouched; `tests/api/test_purge_trigger_router.py` covers 202-queue, dedup-hit, 404/400/401, and the `#263` int64 boundary. Migration 0014 is covered on both a manual connection and the real migrate framework (`tests/db/test_migration_0014_purge_kind.py`), and the partial UNIQUE index is proven to raise `IntegrityViolationError` on a duplicate insert (`tests/db/test_jobs_dedup_index.py::TestPurgeInflightUniqueIndex`).

**SEV-2 — the path-safety guard is unit-tested but not wired-tested.** `tests/agent/test_paths.py` is genuinely adversarial against `under_cache_root` itself: real `..` traversal, a real symlink pointing outside the root, and the cache-root-itself case are all asserted to be dropped. Inverting the guard's condition breaks 4 tests (verified). But **no test proves the purge endpoints actually call it.** Replacing `safe = under_cache_root(...)` with `safe = paths` at `src/orchestrator/agent/routers/steam.py:546` **and** at `src/orchestrator/agent/routers/epic.py:163` leaves the full suite at 1676 passed (both verified independently). For the one operation in the system that deletes files, the guard can be silently unhooked without a single red test.

**SEV-3 — "concurrent" dedup is tested strictly sequentially.** `tests/api/test_purge_trigger_router.py::TestDedup::test_concurrent_calls_return_same_job_id` is named for a race it does not create — `r1 = await client.post(...)` fully completes before `r2` starts. Verified: deleting `ON CONFLICT DO NOTHING` from the INSERT at `purge_trigger.py:78` leaves the full suite green, because the sequential app-level `SELECT` at `purge_trigger.py:58-64` always wins first. The DB-level race protection the module docstring advertises ("concurrent POSTs collapse onto one in-flight purge") is asserted by nothing.

**SEV-3 — untested error branches.** `purge.py:42-43` (Epic manifest with empty `cdn_base` → `ValueError`); `purge.py:44-47` (non-numeric Epic `app_id` silently coerced to `0` — note this is also asymmetric with the Steam branch, which raises); `purge.py:84-86` (non-numeric Steam `app_id`); `purge_trigger.py:86-88` (post-INSERT invisibility → 503); `purge_trigger.py:93-95` (`PoolError` → 503, despite `tests/api/test_prefill_trigger_router.py::test_db_failure_returns_503` demonstrating the standard pattern next door). `agent_client.epic_purge` has no non-2xx → `AgentError` test; `steam_purge` does.

**Jobs-list `kind='purge'`:** `TestJobsFilterEnums::test_purge_kind_is_listed` is a live guard — removing `"purge"` from the `JobResponse.kind` Literal fails it (verified). See finding CF-3 for the sibling test that is not.

### 2. Steam new-purchase auto-cover — strong

All three legs are genuinely exercised.

- **Gated sweep validating `unknown` + `owned`.** `tests/jobs/test_sweep_handler.py::test_gated_sweep_includes_unknown_owned_game` drives the exact new-purchase shape and additionally proves the `owned = 1` guard excludes an unowned `unknown` row. **Verified live:** breaking `'unknown'` in `_CANDIDATE_SQL` (`sweep.py:32`) fails 2 tests. Also covered: `full` vs gated SQL selection, malformed-payload fallback, per-game error isolation, `error` outcome not counted as eviction, agent-sourced health gating.
- **Fetcher covering a prefilled app with a `.bin` but no `.shas`.** `test_enumerate_covers_a_prefilled_app_whose_bin_is_in_the_archive` directly encodes the 2026-08-17 eleven-invisible-games incident; `test_enumerate_does_not_refetch_an_app_that_already_has_shas` closes the other side. This is the best-covered file in the change set at the enumeration layer. (The *fetch* layer is not — see area 4.)
- **Selection reconcile re-adding prefilled non-excluded apps.** The logic lives in `src/orchestrator/scheduler/jobs.py` (`enqueue_auto_classify_block`), not in `selection_file.py` as the brief assumed. `tests/scheduler/test_jobs.py::TestAutoClassifyBlockActuation::test_reconcile_readds_prefilled_non_excluded` asserts a prefilled-and-not-excluded app is restored while a prefilled-and-excluded one is not; `test_reconcile_survives_prefilled_apps_error` covers graceful degradation. `reconcile_selection` itself is fully covered including the sort/dedup guarantee — I probed the ordering contract with `sorted(new, reverse=True)` and it fails 7 tests, so that contract is real.

**SEV-3 gaps:** `sweep.py:69`'s `asyncio.Semaphore(settings.sweep_batch_size)` bounds concurrency and is asserted by nothing — verified, replacing it with `Semaphore(10**6)` leaves the suite green. In the agent-side reconcile wiring, `agent/routers/steam.py:293-296` (corrupt `selectedAppsToPrefill.json` → `"unreadable"`) and `:307-309` (write failure → 500) have no test.

### 3. Steam shared-redist exclusion (228981–228990) — SEV-3, real but boundary-blind

**Genuinely exercised, not tautological.** Both `tests/agent/test_steam_validate.py:361::test_validate_excludes_shared_redist_depot` and `tests/agent/test_steam_purge.py:112::test_steam_purge_skips_shared_redist_depot` write real `.shas` manifests plus real cache chunk files for a redist depot alongside the game's own depot, hit the real HTTP endpoint, and assert real outcomes (`cached` not `partial`; redist files still on disk after purge). **Verified as my positive control:** shrinking the range to `range(228981, 228990)` fails both tests. Good tests.

**SEV-3 — only 228990 is ever used, so half the range boundary is unguarded.** No test uses `228981` (bottom of range), `228980`, or `228991`. **Verified:** widening the constant to `range(228980, 228991)` — an off-by-one that would wrongly exclude a real depot from validation *and* protect it from purge — leaves the full suite at 1676 passed. Only errors that happen to drop 228990 specifically are caught.

**SEV-3 — the operator override path is completely untested.** `settings.py:361-370` `_parse_shared_redist_depots` parses the comma-separated `ORCH_STEAM_SHARED_REDIST_DEPOTS` env var. `tests/core/test_settings.py` contains no reference to `redist` or `228`. The string-parsing branch an operator would actually use has zero coverage.

### 4. Steam manifest fetcher — SEV-2, the headline behaviour is untested

**Genuinely exercised, and impressively so in two places:**

- **Single-flight guard — real concurrency, not a state check.** `tests/agent/test_steam.py:363-427` uses a `_SlowDriver` (`await asyncio.sleep(0.3)`) with a persistent TestClient portal so a background task is genuinely still in flight when the second request lands, and asserts `driver.calls` length to prove one invocation. Covers dedup-onto-in-flight, different-app conflict, and force-vs-non-force.
- **Process-group kill — real, black-box.** `tests/platform/steam/test_prefill_driver.py:179,199` spawn an actual shell script that forks a background child, and verify via a marker file the child writes after a delay that the child did **not** survive — proving `os.killpg` reaches the whole group rather than just the direct child. This is exactly the right way to test it.

**SEV-2 — "find every depot's manifest, not just the primary" is not tested at the layer that implements it.** The discovery loop is `manifest_fetcher.py:215` (`manifest_paths = list(scratch_path.rglob("*.manifest"))`) through the per-file parse at `:230-240`. No test ever lets a subprocess write one or more real `.manifest` files into the scratch dir and checks the returned `[(depot_id, gid, shas), ...]` list:

- The two tests that reach `_run_manifest_only` (`test_run_manifest_only_includes_username_in_argv`, `..._pins_home_to_config_dir`) deliberately produce **zero** manifest files and swallow the resulting `RuntimeError`. They assert argv and env only.
- Every `fetch_all`-level test uses `_fetcher_with_fake_dd`, which **replaces `_run_manifest_only` wholesale** with a lambda returning canned tuples, bypassing the method entirely.

**Verified:** appending `[:1]` to the `rglob` result — reducing the fetcher to primary-depot-only, which is the exact class of bug that produced the `no_manifest_in_cache` coverage gap — leaves the full suite at 1676 passed. Multi-depot discovery *is* proven one layer up in `tests/agent/test_manifest_locator.py`, but that reads already-materialized files; it cannot catch a fetcher that never wrote them.

**SEV-3:** the SIGKILL escalation branch in `prefill_driver.py::_kill_process_group` (reached only if the group ignores SIGTERM for 60 s) is untested and effectively untestable — the 60 s timeout is hardcoded, not injectable, and no fixture script traps SIGTERM.

### 5. Epic prefill — SEV-3

**Correction to the brief:** there is no 404-is-dead / 403-is-transient classifier anywhere in `src/`. The only status-code decision is `epic_downloader.py:115` — `if resp.status_code < 500: break` — which treats all 4xx identically. `_summarize_failures`/`_failure_suffix` (`prefill.py:33-43`) build a diagnostic histogram embedded in `last_error` for a human operator to act on with `game block`. The taxonomy is a logging convention, not a code path, so the question of whether it is "driven through the handler" does not apply.

**Genuinely exercised.** `tests/prefill/test_epic_downloader.py::test_4xx_not_retried` uses a real `httpx.MockTransport`, asserts exactly one call and `chunks_failed == 1`. **Verified:** inverting the `< 500` condition fails 2 tests. 5xx-retry, transport-error retry, and decode-error-does-not-abort are all real. Handler-level `failed` status transitions are covered in `tests/jobs/test_epic_handlers.py` and `test_prefill_handler.py`.

**SEV-3 — the failure histogram is only unit-tested in isolation.** The two handler-level failure tests assert `last_error is not None`, never its content. Nothing drives a real `http 403`/`http 404` chunk failure through `_epic_prefill_inner` and asserts the histogram reaches the `games.last_error` column — which is the entire operator-facing purpose of the feature.

**SEV-3 — wall-clock cron pinning tests only static config.** `tests/scheduler/test_manager.py:315-408` checks the trigger class is `CronTrigger` and that the cron string's hour/minute fields parse. No test fires the scheduler across simulated time — no `freezegun`/`time_machine` anywhere in `tests/scheduler/`, no assertion on `next_run_time`, no clock-jump or missed-window simulation. Crucially, `misfire_grace_time: None` and `coalesce: True` in `manager.py:124-131` — the actual "always fire late rather than skip, and replay once rather than burst" mechanism — are asserted by nothing (verified, finding CF-7). DST is genuinely a non-issue here since the crons are hardcoded `timezone="UTC"`; missed-window catch-up is not.

### 6. CLI/API edge cases (#260/#263/#264/#265) — strong, two SEV-3 gaps

- **#263 — solid, and defensively designed.** Exact boundaries are driven at runtime: `tests/api/test_games_router.py:925-956` covers `2**63` → 400, `-(2**63)-1` → 400, `2**63-1` → 404 (accepted by validation), and `0` → 400; per-router mirrors exist for prefill, validate, and purge. Note the production lower bound is `ge=1`, not `INT64_MIN` — ids ≤ 0 are rejected outright, which is a deliberate documented contract change. `tests/api/test_path_param_bounds.py` adds an OpenAPI-schema sweep across every route **plus** `test_sweep_finds_every_known_game_id_route`, an explicit canary against the vacuous-green failure mode. That is exactly the right instinct and it is rare; it deserves calling out as the model for the rest of the suite.
- **#264 — exceptionally thorough.** LIKE-metacharacter escaping (`%`, `_`, backslash, and escape-of-escape ordering) is covered in `tests/api/test_query_helpers.py` with both explicit cases and Hypothesis property tests that assert every embedded backslash count stays odd. `MAX_CONTAINS_LENGTH` is tested at and over the boundary. The CLI truncation footer is covered for truncated, non-truncated, next-offset-math, and missing-meta cases in `tests/cli/test_cmd_game.py`.
- **#265 — mostly strong.** Bodiless, null-detail, whitespace-only-detail, whitespace-only-body, hostile `reason_phrase`, and list-shaped detail are all covered in `tests/cli/test_client.py:160-243`.
  - **SEV-3:** `_DETAIL_MAX` truncation (`cli/client.py:36,69,72`) is never driven with an over-length body despite the code's own stated motivation ("an arbitrarily large HTML page from a reverse proxy"). **Verified:** raising `_DETAIL_MAX` to `10**9` leaves the full suite green.
  - **SEV-3:** the final fallback `resp.text.strip()[:_DETAIL_MAX]` is only exercised with whitespace-only bodies (which strip to `""`). No test supplies genuine non-JSON text (e.g. an nginx `502 Bad Gateway` page) to confirm it is actually surfaced.
- **#260 — solid.** `tests/cli/test_cmd_game.py:120-141`'s `_detail` mock returns 404 for anything that is not `/api/v1/games/{digits}`, so a regression to a list-scan would fail `test_game_show_found`, `test_game_block_resolves_and_posts`, and `test_game_unblock_resolves_and_deletes`.

### 7. Manual downloads (#222) — good, two SEV-3 gaps

Scope note: the launcher registry, file-normalizer, and alias map from `#252` live in the **Game_shelf** repo, not here. This repo has the API proxy, the agent lister, and the client RPC.

**Genuinely exercised.** Space-and-dot launcher names are tested at all three layers — API (`test_accepts_space_launcher_and_forwards_include_files`), agent (`test_accepts_launcher_with_space_and_dot`), and wire encoding (`tests/clients/test_agent_client.py:489-500` asserts the exact paths `/v1/manual-downloads/Amazon%20Games` and `/v1/manual-downloads/Itch.io?include_files=true`). Path traversal is tested at the agent with `("..", "../cache", "GOG/..", "%2e%2e")`. **`include_files` forwarding is live end-to-end — verified:** stubbing out the query-string append in `agent_client.py:275-276` fails a test.

**SEV-3 gaps.** No test anywhere sends a URL-encoded slash (`%2F`) as or inside a launcher value; the `_LAUNCHER_RE` regex and `quote(launcher, safe='')` both defend against it, but neither defense is exercised adversarially. No test uses a purely-dot launcher (`"."` / `".hidden"`) against the defense-in-depth `target.parent != root` check at `agent/routers/manual_downloads.py:44`; `".."` is covered, `"."` is not.

**Non-coverage note:** the comment at `agent/routers/manual_downloads.py:20-22` claims the regex allows "NO `.` or `/`". The regex on the next line is `^[A-Za-z0-9 ._-]+$`, which allows both space and dot. Stale comment, worth a one-line fix.

### 8. Agent background-task failure surfacing — SEV-2

This is the weakest area, and it reproduces the project's own documented bug class.

**What is covered:** `tests/agent/test_background.py` covers success-logs-nothing, `RuntimeError`-logs-`agent.background_task_failed` with `error`/`error_type`, and the strong-reference-release contract. `tests/agent/test_steam.py:249::test_fetch_manifests_records_failure` covers one of the three `set_failed` sites, with an ordinary `Exception`.

**SEV-2 — two of three failure-recording branches have zero coverage.** All three fire-and-forget wrappers persist failure into `AgentJobStore` behind `except Exception`:

| Site | `set_failed` tested? |
|---|---|
| `agent/routers/steam.py:143` (prefill `_run()`) | **No test at all** |
| `agent/routers/pull.py:92` (pull `_run()`) | **No test at all** |
| `agent/routers/steam.py:247` (fetch-manifests `_run()`) | Yes, `RuntimeError` only |

**Verified:** replacing the prefill handler's `except Exception as e: store.set_failed(...)` with `except Exception: pass` leaves the full suite at 1676 passed. Same for `pull.py`. In production this means a failed prefill or pull leaves the job reading `"running"` forever from the control plane's point of view. The prefill site's `finally` block (which clears `gate["job_id"]` so the next prefill is not wedged behind a dead one) is likewise unverified.

**SEV-3 — the `BaseException`-escapes-`except Exception` shape is untested.** This project has shipped this bug before: UAT-11 / PR #156, where a `gevent.Timeout` (a `BaseException`) escaped `except Exception` and surfaced as `WorkerDiedError`. All three sites above still catch only `Exception`. No test drives a non-`CancelledError` `BaseException` through any of them.

---

## Tests that cannot fail

Every finding below was **verified by mutation**: the named production change was applied to the source, the suite re-run, and the change reverted with `git checkout --`. Unless noted, the result is against the **full** suite. Controls confirming the harness detects real breakage are listed at the end.

| # | Production mutation | Location | Result |
|---|---|---|---|
| CF-1 | `safe = under_cache_root(...)` → `safe = paths` | `agent/routers/steam.py:546` | **1676 passed** |
| CF-2 | `safe = under_cache_root(...)` → `safe = paths` | `agent/routers/epic.py:163` | **1676 passed** |
| CF-3 | `kind: Literal[...8 kinds...]` → `kind: str` | `api/routers/jobs.py:84` | **1676 passed** |
| CF-4 | `rglob("*.manifest")` → `rglob("*.manifest")[:1]` | `platform/steam/manifest_fetcher.py:215` | **1676 passed** |
| CF-5 | `except Exception: store.set_failed(...)` → `except Exception: pass` | `agent/routers/steam.py:143` | **1676 passed** |
| CF-6 | same, pull handler | `agent/routers/pull.py:92-93` | 164 passed (`tests/agent/`) |
| CF-7 | `misfire_grace_time: None` → `1`; `coalesce: True` → `False` | `scheduler/manager.py:128-130` | **1676 passed** |
| CF-8 | `frozenset(range(228981, 228991))` → `range(228980, 228991)` | `core/settings.py:191,367` | **1676 passed** |
| CF-9 | Delete `ON CONFLICT DO NOTHING` from the purge INSERT | `api/routers/purge_trigger.py:78` | **1676 passed** |
| CF-10 | `_DETAIL_MAX = 200` → `10**9` | `cli/client.py:36` | **1676 passed** |
| CF-11 | Swap the two branches of the `missing = ... if not resp.content else ...` ternary | `cli/client.py:131` | 117 passed (`tests/cli/`) |
| CF-12 | Drop `AND status != 'validation_failed'` from the purge UPDATE | `jobs/handlers/purge.py:99` | 9 passed (`tests/jobs/test_purge_handler.py`) |
| CF-13 | `sem = Semaphore(settings.sweep_batch_size)` → `Semaphore(10**6)` | `jobs/handlers/sweep.py:69` | 105 passed (`tests/jobs/`) |

### Detail on the ones that matter most

**CF-1 / CF-2 — SEV-2. The purge path guard can be unhooked silently.**
`tests/agent/test_paths.py` tests `under_cache_root` well — real `..` traversal, a real outward symlink, and the root-itself case are all asserted to be dropped, and inverting the guard's condition fails 4 tests. But it calls the function directly, never through a router. Every path constructed by `tests/agent/test_steam_purge.py` and `tests/agent/test_epic_purge.py` is legitimately inside `cache_root` (built with the same hashing production uses), so removing the guard from the call site changes no observable behaviour in any test. The unit is guarded; the wiring is not. For an operation whose entire risk profile is "deletes files on the NAS," that is the wrong half to leave open.
*Fix shape:* one endpoint-level test per platform that seeds a manifest whose enumerated path escapes the cache root (or points at a symlink out of the tree) and asserts the file survives the purge.

**CF-4 — SEV-2. The fetcher's headline behaviour can be reduced to primary-depot-only with no red test.**
"Find every depot's manifest, not just the primary" is the reason this component exists — it is the fix for the `no_manifest_in_cache` coverage gap. Truncating discovery to a single manifest file leaves the suite fully green, because no test ever lets `_run_manifest_only` discover real files: the argv/env tests produce zero manifests and swallow the resulting error, and every `fetch_all` test replaces `_run_manifest_only` with a canned-tuple lambda.
*Fix shape:* one test where the faked DepotDownloader subprocess actually writes two or more `{depot}_{gid}.manifest` files into the scratch dir, asserting the returned list has one tuple per depot.

**CF-3 — SEV-2, and it is a guard that has already gone stale.**
`tests/api/test_jobs_router.py:589::test_job_response_accepts_all_db_job_kinds` carries this docstring:

> *"UAT-9 regression: the /jobs response model dropped manifest_fetch rows (its kind Literal was stale), silently hiding them from the API. The model's allowed kinds must match the jobs.kind DB CHECK constraint."*

It then hardcodes a list of **six** kinds and asserts only that each is *accepted*. The DB CHECK constraint in `0014_jobs_kind_purge.sql` has **eight**: `fetch_manifests` and `purge` were added and the test was never updated. Because it only tests acceptance, never rejection or set-equality, it cannot detect:
- a kind added to the DB but not the model — the exact UAT-9 bug it exists to prevent, and the case where it has already silently gone stale;
- the Literal being widened to `str` (verified: full suite green).

A sibling test, `TestJobsFilterEnums::test_purge_kind_is_listed`, *is* live — dropping `"purge"` from the Literal fails it (verified, 1 failed). So `purge` specifically is protected by accident of a different test; the general guard is not.
*Fix shape:* read the kind list out of the DB CHECK constraint (or a single shared constant) and assert set-equality with the Literal's `get_args`, plus one assertion that an unknown kind raises `ValidationError`.

**CF-5 / CF-6 — SEV-2. Two of three background failure-recording branches are untested.**
See area 8. A failed prefill or pull silently leaves the job `"running"` forever; nothing turns red.

**CF-9 — SEV-3. The concurrency claim is asserted by a sequential test.**
`test_concurrent_calls_return_same_job_id` issues two fully-serialized `await client.post(...)` calls. The app-level `SELECT` dedup at `purge_trigger.py:58-64` catches the second one before the INSERT runs, so the `ON CONFLICT DO NOTHING` + partial UNIQUE index that the docstring credits for race-safety are never load-bearing in the test. The index itself *is* separately proven to reject a duplicate insert (`tests/db/test_jobs_dedup_index.py`) — but nothing proves the API path relies on it.

**CF-7 — SEV-3.** `test_scheduled_prefill_uses_cron_not_interval` proves the trigger *type* only. The `job_defaults` that determine what happens when a fire is missed are invisible to the entire suite. Combined with the in-memory job store (no persistence across container restarts), missed-fire behaviour is both untested and, by design, unrecoverable — worth flagging to the Orchestrator as a design question, not just a test gap.

**CF-8 — SEV-3.** Only depot `228990` is ever exercised. An off-by-one at either end of `range(228981, 228991)` that does not happen to drop 228990 is invisible. Widening to include `228980` — which would wrongly hide a real depot from validation *and* shield it from purge — is fully green.

**CF-11 — SEV-3.** `cli/client.py:131` picks between two deliberately distinct diagnoses: `"no response body"` (points at a wrong `--url`/proxy) versus `"no error detail in response"` (the API answered but explained nothing). The code comment explains at length why the distinction matters. No test asserts which phrase renders for which case; both `test_bodiless_error_names_the_request_instead_of_trailing_a_colon` and `test_whitespace_only_body_is_treated_as_empty` only check that the request path appears and the message does not end in a colon.

### Candidate investigated and cleared

`platform/steam/selection_file.py:62`'s `sorted(new)` initially looked unguarded — replacing it with `list(new)` is green. That is an artifact of CPython hashing small ints to themselves, so a small-int set iterates in sorted order anyway. The real probe, `sorted(new, reverse=True)`, **fails 7 tests**. The ordering contract is genuinely covered. Reported here so it is not re-raised.

### Controls (proving the harness detects real breakage)

| Control | Result |
|---|---|
| `range(228981, 228991)` → `range(228981, 228990)` (drops 228990) | 2 failed |
| `if resp.status_code < 500:` → `>= 500:` (`epic_downloader.py:115`) | 2 failed |
| `if root in resolved.parents:` → `if True:` (`agent/_paths.py:34`) | 4 failed |
| `sorted(new)` → `sorted(new, reverse=True)` (`selection_file.py:62`) | 7 failed |
| Drop `'unknown'` from `_CANDIDATE_SQL` (`sweep.py:32`) | 2 failed |
| Drop `"purge"` from `JobResponse.kind` Literal | 1 failed |
| Stub out `include_files` query-string append (`agent_client.py:275`) | 1 failed |

**Repo state:** every mutation was reverted with `git checkout -- src/`, verified `0` dirty files under `src/` after each batch. `git status --short` at completion shows only pre-existing/sibling-agent changes (`.claude/manifest.json`, and another UAT-14 agent's `exploratory.md`). No source, test, or config file was left modified.

---

## Timing and flakiness

### Timing

| Run | Wall time | Result |
|---|---|---|
| 1 | 44.18 s | 1676 passed, 3 deselected |
| 2 | 43.13 s | 1676 passed, 3 deselected |

Slowest tests (`--durations`), all legitimate:

```
2.52s  tests/platform/steam/test_prefill_driver.py::test_timeout_kills_the_whole_process_group
2.27s  tests/scripts/test_phase_gate_script.py::test_no_sigpipe_under_pipefail
1.58s  tests/scripts/test_phase_gate_script.py::test_no_local_outside_function
1.51s  tests/platform/steam/test_prefill_driver.py::test_cancelling_the_run_kills_the_process_group
1.23s  tests/db/test_pool_reader_exhaustion.py::test_reader_pool_recovers_after_fault_clears
1.11s  tests/api/test_health_endpoint.py::TestHealthDynamicFields::test_uptime_sec_increases_monotonically
```

No single test dominates. 44 s for 1676 tests is healthy.

### Flakiness

**None observed.** Two consecutive full runs produced byte-identical summary lines. Hypothesis-driven property tests in `test_pool_property.py` and `test_query_helpers.py` passed in both.

One structural caveat: **`pytest-randomly` is not installed**, so collection order is fixed and identical on every run. Two runs in the same order do not test order-independence. Inter-test state leakage — a fixture that does not clean up, a module-level singleton — would not be detected by this suite as configured. Worth one `-p randomly` run at some point; not a finding on its own.

### SEV-2 — the documented run command hangs indefinitely on a developer machine

**My first two attempts at the documented command did not terminate**, one killed at 10 minutes and one at ~7. Both stalled at the identical point, ~98% through collection order. This is not intermittent — it is deterministic given a wedged local Docker/Colima daemon, and it cost more wall-clock time than the entire rest of this audit.

The chain, traced by process tree and by sampling the blocked interpreter (`select_poll_poll` → `poll` in `libsystem_kernel`, i.e. blocked on a subprocess pipe):

```
pytest
└── tests/scripts/test_phase_gate_script.py::test_no_sigpipe_under_pipefail
    └── bash scripts/check-phase-gate.sh
        └── bash scripts/resolve-tools.sh            # line 480 gate
            └── eval "$TOOL_VERSION_CMD"             # resolve-tools.sh:200
                └── colima version                   # templates/tool-matrix/common.json:118
                    └── limactl shell --instance colima sudo cat /etc/colima/colima.json
                        └── ssh -o ControlPath=.../colima/ssh.sock   ← blocks forever
```

Three things combine:

1. **`resolve-tools.sh:200` `eval`s arbitrary third-party version commands with no timeout**, and swallows their stderr (`2>/dev/null`). Roughly forty tools in `templates/tool-matrix/common.json` are probed this way. Any one of them that blocks — a wedged container daemon, an unreachable VM, a credential prompt with no tty — blocks the test suite forever with no diagnostic.
2. **`check-phase-gate.sh:480` guards the resolver with `[ -z "${CI:-}" ]`.** CI sets `CI=true`, so this never runs there. `tests/scripts/test_phase_gate_ci_guard.py` sets `CI=true` explicitly and is fine. But `tests/scripts/test_phase_gate_script.py`'s two tests deliberately run *without* `CI`, which is what makes them the ones that hang. **The failure is structurally invisible to CI and lands only on the developer.**
3. The proximate trigger here was a stale `limactl shell … docker ps -q` held by the Colima daemon (14+ minutes old), which wedged the lima ssh ControlMaster. Killing it unblocked exactly one test; the next `_run()` re-wedged it.

I completed the audit by shimming `colima` on `PATH` (in a scratch directory, outside the repo — no repo file was touched) so the version probe returns immediately. With that in place the suite runs in 44 s, which is the honest baseline reported above. **The 1676/1676 green result is real; it is only reachable on this machine with that workaround.**

*Fix shape (cheapest first):* wrap the `eval` at `resolve-tools.sh:200` in a `timeout 5` — the version string is cosmetic and an empty result already has a defined meaning in the script. Alternatively, set `CI=true` in `tests/scripts/test_phase_gate_script.py`'s `_run()` and add a separate non-CI test that stubs `PATH` to a directory of fast fakes. Either way, the suite should not be able to hang on the health of an unrelated local daemon.

---

## Findings index

| ID | Sev | Finding |
|---|---|---|
| A-1 | SEV-2 | Purge path-safety guard (`under_cache_root`) can be removed from both `steam_purge` and `epic_purge` call sites with the full suite green (CF-1, CF-2) |
| A-2 | SEV-2 | Steam manifest fetcher's multi-depot discovery loop can be truncated to a single depot with the full suite green (CF-4) |
| A-3 | SEV-2 | `test_job_response_accepts_all_db_job_kinds` hardcodes a stale 6-kind list against an 8-kind DB constraint, tests only acceptance, and cannot detect a widened Literal — the exact UAT-9 regression it was written to prevent (CF-3) |
| A-4 | SEV-2 | Background failure recording (`set_failed`) is untested at 2 of 3 sites; a failed prefill or pull leaves the job `"running"` forever with nothing red (CF-5, CF-6) |
| A-5 | SEV-2 | The documented full-suite command hangs indefinitely on a developer machine via `tests/scripts` → `resolve-tools.sh` → untimed `eval` of `colima version`; structurally invisible to CI |
| A-6 | SEV-2 | Three DB-pool endurance tests are permanently deselected by an unconditional `addopts = ["-m","not slow"]`, invisible to `-rs`, with no compensating job |
| A-7 | SEV-3 | Purge in-flight dedup: `ON CONFLICT DO NOTHING` removable with suite green; the "concurrent" test is strictly sequential (CF-9) |
| A-8 | SEV-3 | Shared-redist exclusion tested only at 228990; range boundary and the `ORCH_STEAM_SHARED_REDIST_DEPOTS` parser both unguarded (CF-8) |
| A-9 | SEV-3 | Scheduler `misfire_grace_time` / `coalesce` asserted by no test; missed-fire behaviour untested and, with an in-memory job store, unrecoverable by design (CF-7) |
| A-10 | SEV-3 | `_DETAIL_MAX` truncation and the two bodiless-error diagnostic phrases both unasserted (CF-10, CF-11) |
| A-11 | SEV-3 | Sweep concurrency semaphore removable with suite green (CF-13) |
| A-12 | SEV-3 | Epic failure histogram never asserted to reach `games.last_error`; only `last_error is not None` is checked |
| A-13 | SEV-3 | Untested error branches in `purge.py` (empty `cdn_base`, non-numeric app_ids) and `purge_trigger.py` (503 paths); `epic_purge` lacks the non-2xx→`AgentError` test `steam_purge` has |
| A-14 | SEV-3 | Manual downloads: no `%2F` and no bare-`"."` launcher adversarial input; stale comment at `agent/routers/manual_downloads.py:20-22` |
| A-15 | SEV-3 | `BaseException`-escapes-`except Exception` untested at all three agent background sites, despite UAT-11/PR #156 having shipped exactly that bug |
| A-16 | SEV-3 | No `pytest-randomly`; fixed collection order means inter-test state leakage would go undetected |

**Recommended priority:** A-5 first (it blocks everyone's local workflow and is a two-character fix), then A-1 and A-4 (both concern destructive or silently-failing operations), then A-2 and A-3.

A closing note on what is *good*, since it should not be lost in a list of gaps: `tests/api/test_path_param_bounds.py` includes an explicit canary against its own vacuous-green failure mode, and `tests/platform/steam/test_prefill_driver.py` proves process-group kill black-box via a marker file a forked child would have written. Both are the standard the findings above should be held to, and both were written in this codebase.
