# Security Audit — Force prefill option

**Feature:** force-prefill (`--force` / `?force=true`)
**Modules:**
- `src/orchestrator/api/routers/prefill_trigger.py` — `force` query param, payload write, dedup force-upgrade
- `src/orchestrator/jobs/handlers/prefill.py` — `_payload_force()`, force threaded through `_steam_prefill`/`_steam_prefill_inner`
- `src/orchestrator/cli/client.py` — `OrchClient.post(..., params=)`
- `src/orchestrator/cli/commands/game.py` — `game prefill --force` / `-f`
**Audit date:** 2026-06-29
**Auditor:** self-review (Senior Security Engineer persona) + ruff (incl. flake8-bandit `S` rules) + mypy --strict + full test suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-06-29 -->

## Scope

Post-implementation security review of the force-prefill capability: a `force`
flag that threads SteamPrefill `--force` (re-request every chunk) so the
orchestrator can refill a game left `partial` by lancache eviction. The flag
travels API → job `payload` → worker → handler → driver/agent. No new
dependency, no schema migration (reuses the existing `jobs.payload` TEXT column).

10 new tests across `tests/jobs/test_prefill_handler.py` (4),
`tests/api/test_prefill_trigger_router.py` (4), `tests/cli/test_cmd_game.py` (2).

## Methodology

1. **SAST-lite.** `ruff check src/orchestrator tests` (flake8-bandit `S` rules
   enabled — incl. `S101` no-assert, `S608` SQL-injection heuristics) — clean.
2. **Type safety.** `mypy src/orchestrator` — clean (88 source files).
3. **Threat-model cross-check** against the project threat model: SQL injection
   (parameterized), auth boundary (bearer), resource-exhaustion / DoS
   amplification, log credential leak.
4. **Test verification.** Full suite `pytest -q --ignore=tests/scripts` — 1295
   passed (only the documented `tests/test_licenses.py` local-tooling failure).
   Force-specific behavior (payload set, default NULL, dedup-upgrade queued vs
   running, handler parse incl. malformed-payload, CLI param) covered by the 10
   new tests.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (explicitly checked, clean)

- **SQL injection.** The trigger's INSERT/UPDATE/SELECT all use `?` parameter
  binding. The only value the feature adds to SQL is the payload, bound as a
  parameter and sourced from a **fixed module literal** `_FORCE_PAYLOAD =
  '{"force": true}'` — never from request data. No user-controllable string
  reaches SQL composition.
- **No injection via the flag.** `force` is parsed by FastAPI as a strict
  `bool` query param (`?force=true`); a non-bool value yields a 422, never
  reaches the DB. The CLI sends the literal string `"true"`; the boolean comes
  from a Click `is_flag` option (no free-form value).
- **Payload parse is crash-safe (DoS via malformed payload).**
  `_payload_force()` wraps `json.loads` in `try/except (ValueError, TypeError)`
  and type-checks the parsed object is a `dict` before `.get("force")`. A NULL,
  non-JSON, non-object, or oversized payload returns `False` — it cannot raise
  out of the worker loop or flip force on unexpectedly. Verified by
  `test_steam_prefill_malformed_payload_is_false` and
  `test_steam_prefill_payload_without_force_is_false`.
- **Auth boundary unchanged.** `POST /api/v1/games/{id}/prefill` remains
  bearer-gated (`test_missing_bearer_returns_401`, `test_wrong_bearer_returns_401`
  still green). The feature adds no new endpoint and no new unauthenticated
  surface. The CLI talks to the same bearer-gated API.
- **Resource-exhaustion / DoS amplification.** A forced prefill is more
  expensive than a normal one (re-requests every chunk → LAN reads + WAN for the
  evicted subset). Amplification is bounded: (a) the caller must already hold the
  orchestrator bearer, with which they can already enqueue prefills; (b) the
  migration-0006 in-flight UNIQUE index permits at most one prefill per game, so
  force cannot stack duplicate concurrent runs; (c) force never re-downloads a
  whole game from the internet — lancache serves still-cached chunks as LAN hits.
  Accepted: no privilege boundary is crossed that the bearer didn't already grant.
- **Dedup force-upgrade is state-guarded.** The upgrade only rewrites the payload
  of a **queued** job (`existing["state"] == "queued"`); a **running** prefill is
  returned unchanged (it cannot be mutated mid-run). The UPDATE binds the job id
  as a parameter and is idempotent (`!= _FORCE_PAYLOAD` guard avoids a redundant
  write). Verified by `test_force_upgrades_queued_nonforce_job` and
  `test_force_does_not_upgrade_running_job`.
- **No secret exposure in logs.** The only new log field is `force=<bool>`
  (`prefill.started`, `prefill_trigger.force_upgraded`). No token, password,
  Steam Guard, or session material is read or logged by this feature. The
  SteamPrefill driver's existing token-redaction is unchanged.
- **No dead/misleading flag re-introduced.** This deliberately revives a per-job
  force the 2026-06-23 CORE-2 cleanup removed *as dead* (it read a top-level job
  key the row never carried). The new force is sourced from the `payload` column
  the worker actually `SELECT`s, and the CORE-2 guard tests
  (`..._ignores_dead_force_key`) remain green — a stray top-level `force` key is
  still ignored; only `payload.force` is honored.
- **Steam-scoped.** Force is consumed only in the steam path; the Epic prefill
  path ignores it (no behavior change, no Epic `--force` semantics assumed).

## Decision

**Force-prefill is cleared to advance through the Build Loop.** No SEV-1/2/3/4
findings. The change is additive, bearer-gated, parameterized, crash-safe on
malformed input, and bounded against amplification by the existing per-game
in-flight dedup. Covered by 10 new tests; ruff + mypy clean; full suite green.

## Sign-off

- Implementation: commit `<pending>` (appended after the green-phase commit lands)
- Test suite: 1295 passed (`--ignore=tests/scripts`); 10 new force tests
- ruff (incl. bandit `S` rules) + mypy clean on the four changed modules
- No new dependency; no migration (reuses `jobs.payload`)
