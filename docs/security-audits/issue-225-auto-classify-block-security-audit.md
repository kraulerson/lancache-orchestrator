# Security Audit — #225 auto-classify-block (post-download prefill exclusion)

**Feature:** issue-225-auto-classify-block — after a game is prefilled, auto-exclude classifier-flagged non-games from future scheduled prefill; operator override (allow/exclude).
**Modules:**
- `db/migrations/0011_prefill_exclusions.sql` — new `prefill_exclusions` table (mode exclude|allow, source classifier|operator)
- `scheduler/jobs.py::enqueue_auto_classify_block` — the post-download classify+exclude step; `enqueue_scheduled_prefill` skip clause
- `scheduler/manager.py`, `core/settings.py`, `api/main.py` — cron registration + `auto_classify_block_enabled` flag
- `api/routers/prefill_exclusions.py` — GET/POST/DELETE operator override (bearer-gated)
- `cli/commands/selection.py` — `selection allow|exclude|unset|exclusions`
**Audit date:** 2026-07-04
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy + full suite (1451)
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-07-04 -->

## Scope

The scheduled prefill keeps downloading everything; a new cron step classifies owned Steam games that have been prefilled at least once (via the #229 classifier over `steam_app_info`) and inserts an `exclude` row into `prefill_exclusions` for soundtracks/tools/servers/demos. The prefill selection gains a `NOT EXISTS(... mode='exclude')` clause. The operator can flip a game to `allow` (sticky — the auto step's `ON CONFLICT DO NOTHING` + the `NOT EXISTS` candidate filter never overwrite it).

## Methodology

1. **SAST-lite.** `ruff check src/orchestrator tests` (flake8-bandit `S`) — clean.
2. **Type safety.** `mypy src/orchestrator` — clean (97 files).
3. **Migrations.** `tests/db/test_migrate.py` green — 0011 applies, table auto-added to the post-apply expected-tables check.
4. **Tests.** New: scheduler (exclude-skip, allow-no-skip, auto-classify insert/idempotent/never-prefilled/unowned/allow-untouched/pool-error), API (list/set/upsert/400/422→400/delete/401), CLI (allow/exclude/unset/exclusions/bad-spec). Full suite 1451.
5. **Threat-model cross-check:** SQL injection, authz, availability (never-raises), destructive-safety.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (explicitly checked, clean)

- **Non-destructive — never deletes cache.** The feature only writes rows to `prefill_exclusions` and only affects *whether the scheduler enqueues a prefill*. A misclassified game is downloaded once (already cached) and simply not re-prefilled; it stays on disk and is fully recoverable via `selection allow`. There is no `os.unlink`/cache write anywhere in the change.
- **No SQL injection.** All writes/reads are parameterized (`?`). `enqueue_auto_classify_block` binds `platform`/`app_id`/reason as parameters; the reason string is `f"auto-classify: {classify(...)}"` where `classify` returns a fixed tag (`type=music`, `name~'...'`) — not attacker-controlled, and it's a bound parameter regardless. The prefill skip clause is a static literal.
- **No ReDoS.** Reuses the #229 `classify()` (flat literal-alternation regex, linear time).
- **Availability.** `enqueue_auto_classify_block` follows the scheduler-callback contract: wrapped in `try/except PoolError`/`except Exception`, returns 0, never raises (a raised callback degrades APScheduler). Per-row inserts are bounded (only classifier-flagged rows) and idempotent (the `NOT EXISTS` filter + `ON CONFLICT DO NOTHING` mean a second run inserts nothing).
- **Authz.** The override router sits under the same bearer middleware as every other `/api/v1` write (`test_no_token_401`). `platform` is validated against `{steam,epic}` (400) and `mode` against a Pydantic `Literal` (→ global 400). The CLI reaches it over loopback with the operator token.
- **Sticky override cannot be clobbered by automation.** The auto step's candidate query filters `NOT EXISTS (prefill_exclusions)`, so any operator row (allow or exclude) removes the game from re-classification; the belt-and-suspenders `ON CONFLICT DO NOTHING` covers a race. An operator `allow` is therefore never overwritten by the classifier.

## Decision

No findings. Ship.
