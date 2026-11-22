# Security Audit — New - Not Cached (Feature 31, orchestrator half)

**Feature:** new-not-cached. It records `games.first_seen_at`, runs a 6-hourly
`new_games_check` job plus a SteamPrefill download watch, pushes two Kuma monitors,
and exposes `first_seen_at` and `is_new` on the games API.

**Modules:**
- `src/orchestrator/db/migrations/0019_new_games_check.sql`: adds `games.first_seen_at`, rebuilds `jobs` with the new kind and source, and adds the in-flight index.
- `src/orchestrator/jobs/new_games.py`: the check-set and overdue-set SQL, and the Kuma message.
- `src/orchestrator/jobs/handlers/new_games_check.py`: the job handler.
- `src/orchestrator/jobs/worker.py`: re-reads `source` before the heartbeat.
- `src/orchestrator/scheduler/jobs.py`, `scheduler/manager.py`, `scheduler/steam_download_watch.py`: scheduling.
- `src/orchestrator/api/routers/new_games_trigger.py`: `POST /api/v1/new-games-check`.
- `src/orchestrator/api/routers/games.py`: the `first_seen_at` and `is_new` fields.
- `src/orchestrator/core/settings.py`: 7 new settings.
- `src/orchestrator/cli/commands/cache.py`: `cache check-new`.

**Audit date:** 2026-11-21

**Auditor:** final whole-branch adversarial review (opus, max effort, Senior Security
Engineer persona), plus a scoped re-review of the fix wave. Tools: bandit, semgrep
1.184.0, ruff (flake8-bandit `S`), mypy --strict, the full suite, and the real
migration runner.

**Phase:** 2 (Construction), Build Loop step 2.4.

<!-- Last Updated: 2026-11-21 -->

## Scope

A recently bought game shows "New - Not Cached" until a check measures it cached. A
new game still uncached after 7 days, or still `unknown` 48 h after it was first
seen, turns a Kuma monitor DOWN. The design is
`docs/superpowers/specs/2026-11-20-new-not-cached-design.md` (Feature A).

## Methodology

1. **SAST.**
   - semgrep 1.184.0 with `p/owasp-top-ten`, `p/security-audit` and the repo's `.semgrep/` rules: 0 findings.
   - bandit on the changed files: B608 on `new_games.py:39,49`, judged a low-confidence false positive (see below).
   - `ruff check src tests`: clean.
2. **Type safety.** `mypy --strict src`: no issues in 115 files.
3. **Migration integrity.** The real `run_migrations` was run through 0018 against live-like `jobs` and `games` data, including queued and running rows, then 0019:
   - rows identical;
   - index SQL unchanged, plus the one new index;
   - `foreign_key_check` empty and `integrity_check` ok;
   - no `jobs_backup` left behind, and a re-run is a no-op;
   - the checksum matches CHECKSUMS.
4. **Threat-model cross-check:** injection, authentication and the source-IP allowlist, secret exposure, denial of service, input validation, data exposure, and the single-writer rule.
5. **Tests.** The full suite gives 2233 passed, 3 deselected, 1 pre-existing warning (a Starlette deprecation).

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No security findings. | — |

The final review's correctness findings (I1: liveness was lost behind a trigger
check; I2: overdue games were never re-measured) were not security issues. Both
were fixed and re-reviewed. Two Minor residuals, doc wording and a rare race on a
liveness push, are recorded in the feature's SDD ledger for the commit decision.

## Non-findings (explicitly checked, clean)

- **SQL injection.**
  - The new SQL is built only by joining module-level string constants (`_ELIGIBLE` and the statement bodies) at import time.
  - Every value (the window in days, the alert hours, ids) is bound with `?`.
  - Bandit's B608 on that concatenation is a false positive: no request or row data reaches the SQL text.
  - The one `# noqa: S608` carries a reason comment.
- **Authentication on `POST /api/v1/new-games-check`.**
  - The endpoint is not in `AUTH_EXEMPT_PATHS`, so the app-wide `BearerAuthMiddleware` applies; a test asserts 401 without the bearer.
  - The global source-IP allowlist and the body-size cap also apply.
  - The route takes no body.
- **Denial of service.**
  - The in-flight unique index (`idx_jobs_new_games_check_inflight`) allows at most one queued or running check, so repeated authenticated POSTs cannot pile up jobs.
  - A chained check costs one Steam library sync. That is 31 store calls per sync, measured live on 2026-11-21, within `steam_store_fetch_budget` (150).
  - This is the same class as the existing sweep trigger.
  - `steam_download_poll_sec` must be 0 or at least 60 (validator).
- **Secrets in logs.**
  - The Kuma push URLs are never logged, and a test asserts the URL is absent from `new_games.steam_sync_failed`.
  - The existing log redactor covers `kuma[_-]?push` keys.
  - The bearer token is never logged.
  - `AgentError` messages carry only the exception type, status and path.
  - The agent's LAN URL can appear in a truncated `reason` field, the same as `sweep.game_error`. That is ruling 7, and it is not a secret.
- **Input validation (settings).**
  - Window: 1–60 days. Alert hours: 6–1440. Poll interval: 0 or 60–86400 s.
  - The cron is checked at load by `_checked_cron`.
  - An unset push URL disables that push.
- **Single-writer rule.**
  - The check writes cache status only through `validate_one_game`, then `record_measurement`.
  - `tests/test_measurement_writer_guard.py` is green.
- **Data exposure.**
  - The `excluded` projection is used only to compute `is_new` and is not serialised (tested).
  - The new response fields are additive.
- **Kuma message.**
  - Titles are URL-encoded by httpx query params.
  - The message is capped at `heartbeat.MSG_MAX_CHARS` (200), so it is never tail-trimmed.
- **Migration failure mode.**
  - With foreign keys ON inside one `BEGIN IMMEDIATE`, a dangling `jobs.game_id` makes 0019 roll back to 0018. That fails safe.
  - The live DB has 0 foreign-key violations (`PRAGMA foreign_key_check`, 2026-11-21).
  - The deploy change card re-runs the check on the pre-deploy backup.

## Decision

**Cleared to advance.** There are no SEV-1, SEV-2, SEV-3 or SEV-4 security findings.
The change is additive, the SQL is static with bound values, the endpoint is
authenticated and bounded, no secret is logged, and the migration fails safe.

## Sign-off

- Implementation: uncommitted (commit pending Karl's approval).
- Test suite: 2233 passed, 3 deselected.
- ruff, `ruff format --check` and `mypy --strict` are clean; semgrep reports 0 findings.
- No new dependency. One migration (0019).
