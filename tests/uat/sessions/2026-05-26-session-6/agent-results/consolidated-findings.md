# UAT-6 — Consolidated Agent Findings

**Session:** 6
**Date:** 2026-05-26
**Subject:** BL10 + BL11 (Steam auth substrate + library sync)
**Agents:** automated-suite, exploratory-adversarial, deployment-shape

## Headline

**3 SEV-2 production blockers found.** All three would prevent or corrupt BL11's first run against real Steam. Operator manual session against live Steam is BLOCKED on these — running UAT-6 manually now would just demonstrate the SEV-2s; no signal about other failure modes.

## SEV-2 — Production blockers (must fix before operator session)

### F-UAT6-1: IPC `readline()` 64 KiB limit defeats `library_enumerate`
**Source:** exploratory agent F1
**Files:** `src/orchestrator/platform/steam/client.py:199` (`self._process.stdout.readline()`)
**Mechanism:** `asyncio.subprocess` uses a default `StreamReader._limit = 65536`. When the worker emits a `library.enumerate` response > 64 KiB (typical for any owned-library larger than ~600 apps at ~100 bytes each), `readline()` raises `ValueError("Separator is not found, and chunk exceed the limit")`. The ValueError is NOT caught in `_read_loop`, so the reader task dies. The subprocess is leaked — `_on_worker_died` is never called from this path; restart-storm guard never trips. The caller times out instead of receiving `WorkerDiedError`. The `MAX_IPC_LINE_BYTES = 10 MiB` cap in `protocol.py` is dead code on the response path.
**Impact:** BL11 cannot work against any real Steam account with a meaningful library size. The 624-test suite is silent on this because every BL11 handler test stubs `library_enumerate()` and never crosses the subprocess boundary.
**Fix sketch:** Wire a larger limit when creating the subprocess (e.g., `limit=11 * 1024 * 1024` to allow 1 MiB headroom above the 10 MiB protocol cap) and catch `ValueError` in `_read_loop` → call `_on_worker_died(reason='response_too_large')`. Tests: feed a 128 KiB response line through `_read_loop` and assert it parses cleanly.

### F-UAT6-2: Worker hardcodes session dir, ignores `Settings.steam_session_dir`
**Source:** deployment-shape agent F1
**Files:** `src/orchestrator/platform/steam/worker.py:58` (`_ensure_client(credential_dir: str = "/var/lib/orchestrator/steam_session")`)
**Mechanism:** The worker's `_ensure_client()` accepts the path as a parameter but every call site uses the default. The setting `Settings.steam_session_dir` is never read inside the worker subprocess. Operators deploying with a custom volume mount (e.g., `/data/orchestrator/steam_session` for the lancache host) will silently lose refresh-token persistence across container restarts because steam-next writes to the hardcoded path that isn't volume-mounted.
**Impact:** "Stay logged in across restarts" doesn't work for any operator who customized the path.
**Fix sketch:** Worker reads `os.environ["ORCH_STEAM_SESSION_DIR"]` (with the default as fallback) since it can't import the orchestrator's Settings (separate venv). Orchestrator's client.py adds the env var to the subprocess env dict (currently filters to PATH/LANG/LC_ALL — extend to include `ORCH_STEAM_SESSION_DIR`). Test: assert spawn env contains the var.

### F-UAT6-3: `library_sync` doesn't flip platforms.auth_status on NotAuthenticated
**Source:** deployment-shape agent F2
**Files:** `src/orchestrator/jobs/handlers/library_sync.py` (entire handler)
**Mechanism:** When the worker returns `{kind: 'NotAuthenticated'}`, the client raises `SteamWorkerError`, which propagates up to the jobs worker loop, which marks the job `failed`. But the `platforms.auth_status` stays at `'ok'` from the last successful auth — never flips to `'expired'`. The operator querying `GET /api/v1/platforms` sees `auth_status='ok'` while `GET /api/v1/platforms/steam/auth/status` (which queries the worker) returns `authenticated=False`. The two surfaces disagree.
**Impact:** Operator-facing state inconsistency. F12 scheduled sync (post-MVP) will see auth_status=ok and not prompt re-auth.
**Fix sketch:** library_sync_handler catches `SteamWorkerError` where `kind == 'NotAuthenticated'`, updates platforms row to `auth_status='expired'`, re-raises. Test: stub `library_enumerate` raises NotAuthenticated → assert platforms.auth_status becomes 'expired' after handler exits.

## SEV-3 — Hardening items (triage candidates)

| ID | Source | Summary | File:line |
|----|--------|---------|-----------|
| F-UAT6-4 | exploratory F2 / deployment F4 | Worker `stderr=PIPE` never drained; pipe buffer (~64 KiB on macOS, 4-64 KiB on Linux) can fill on long uptime and deadlock writes | `client.py:107` |
| F-UAT6-5 | exploratory F3 | Auth auto-trigger + manual POST race creates duplicate queued rows; dedup docstring overstates robustness | `auth.py` (auto helper) + `sync.py` |
| F-UAT6-6 | deployment F3 | Unbatched `get_product_info(apps=...)` likely exceeds 30 s IPC timeout for libraries above ~700 apps (heavy LAN-party operator) | `worker.py:218` |
| F-UAT6-7 | deployment F5 | Half-dead `steam_client` is published into DI singleton even when `start()` failed; subsequent 503s are correct but operator-facing logs are confusing | `main.py:111-119` |
| F-UAT6-8 | deployment F6 | Jobs worker can claim a stale `library_sync` queued before a power loss and fail it with NotAuthenticated; no reaper, operator sees no context | `worker.py` (jobs) |

## SEV-4 — Polish / follow-ups

| ID | Source | Summary |
|----|--------|---------|
| F-UAT6-9 | exploratory F5 | Handler logs `str(app)[:200]` on skipped apps — defense-in-depth PII concern if steam-next ever surfaces PII fields |
| F-UAT6-10 | exploratory F6 | Worker silently synthesizes `f"app_{app_id}"` when `common.name` is missing/non-string — pollutes games table with fake names |
| F-UAT6-11 | exploratory F7 | `_steam_client_singleton` is a module-global; leaks across test app instances (test isolation hygiene) |
| F-UAT6-12 | automated H1 | Dev `.venv` ruff is 0.15.11, lockfile pins 0.15.14 — local environment drift after dependabot merge |
| F-UAT6-13 | automated H2 | gevent 26.5.0 / zstandard 0.25.0 unverifiable in dev (steam-worker venv not present locally); real-worker smoke test needed before F1 milestone 3/3 |
| F-UAT6-14 | automated H3 | 3 aiosqlite ResourceWarning (Connection deleted before close) — verified pre-existing on BL10 baseline; backlog |

## Discrepancies / inconsistencies

- **ADR-0013 vs main.py:** ADR-0013 says "spawn worker only if session file exists" but `main.py:111-119` always spawns. Either the ADR or the code is wrong. Code is currently winning. (deployment finding D-1)

## Test coverage gaps surfaced (no immediate code defect)

- No test feeds a >64 KiB line through the real `_read_loop` — would have caught F-UAT6-1
- No test covers jobs-worker mid-handler + lifespan shutdown leaving state='running' orphans (no reaper exists on restart)
- No test for `app_id=""` from steam-next triggering CHECK constraint violation
- No test for unicode/control chars in app names through the full path

## Automated suite gate sign-off

All 6 gates GREEN on current main (post PR #103-#106):
- pytest: 722/722 (1 expected-fail on `test_licenses.py`, pre-existing)
- ruff check / format check / mypy --strict / gitleaks / semgrep p/owasp-top-ten: 0 findings
- No new TODO/FIXME markers in BL11 sources
- No DeprecationWarning / FutureWarning from the dependabot bumps

## Recommended action

Fix the 3 SEV-2s test-first BEFORE running the operator manual session — otherwise the manual session against live Steam will:
1. Hang or time out on `library.enumerate` (F-UAT6-1)
2. Lose session on container restart (F-UAT6-2)
3. Leave platforms.auth_status stale on token expiry (F-UAT6-3)

None of those would produce useful UAT signal. Then re-run the agent sweep (or at least the exploratory agent) to confirm the fixes; then hand the operator template to the operator with the SEV-3/4 items as "things to watch for" callouts.
