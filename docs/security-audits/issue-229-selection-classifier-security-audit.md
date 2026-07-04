# Security Audit — #229 prefill-selection exclusion classifier

**Feature:** issue-229-selection-classifier (classify Steam apps as prefill-exclusion candidates — read-only review, never edits the selection)
**Modules:**
- `src/orchestrator/platform/steam/selection_classifier.py` — pure `classify(app_type, name) -> reason|None`
- `src/orchestrator/api/routers/selection.py` — `GET /api/v1/selection/candidates` (bearer-gated read)
- `src/orchestrator/cli/commands/selection.py` — `orchestrator-cli selection classify`
- `src/orchestrator/api/main.py`, `src/orchestrator/cli/main.py` — registration
**Audit date:** 2026-07-03
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy + full suite (1415)
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-07-03 -->

## Scope

Prefill pulls every app in the operator's `selectedAppsToPrefill.json` to the LAN cache; soundtracks, dedicated servers, SDKs, tools, demos, and videos waste WAN pulls and cache space. This feature classifies the Steam apps already cached in `steam_app_info` (type + name, populated by library_sync) and reports **candidates** for the operator to remove. It is strictly read-only — it never edits the curated selection (the task's own gate).

## Methodology

1. **SAST-lite.** `ruff check` (flake8-bandit `S`) on all modules — clean.
2. **Type safety.** `mypy` on the three feature modules — clean.
3. **Tests.** 26 new (classifier param sweep, router happy/empty/401/503, CLI list/none). Full suite green (1415).
4. **Threat-model cross-check:** SQL injection, ReDoS, authz, info-disclosure, the "never write" gate.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (explicitly checked, clean)

- **Read-only — the selection is never mutated (the gate).** The endpoint issues a single `SELECT app_id, app_type, name FROM steam_app_info`; there is no `INSERT`/`UPDATE`/`DELETE`, no filesystem write, and no call to the agent or SteamPrefill. The candidate list is advisory output only; the operator edits `selectedAppsToPrefill.json` by hand. Enforced structurally (no write path exists in the module).
- **No SQL injection.** The query is a fixed literal with no interpolation and no parameters derived from the request. `classify()` receives values read back from the DB.
- **No ReDoS.** `_NAME_FLAG_RE` is a flat alternation of literal phrases with no nested quantifiers or overlapping ambiguity — linear-time. `_NON_GAME_TYPES` is a frozenset membership test.
- **Authz unchanged.** The route sits under the same bearer middleware as every other `/api/v1` read; `test_no_token_returns_401` confirms rejection before the handler runs. The CLI reaches it over loopback with the operator token, identical to `cache`/`jobs`.
- **No information disclosure.** Error paths return a fixed `{"detail": "database unavailable"}` (503) — no exception text or SQL echoed. The candidate payload contains only `app_id`/`name`/`app_type` (already exposed via the games API) plus a short reason tag.
- **No new secret or network surface.** No credentials, no outbound calls (the store lookup happened earlier in library_sync; this reads the cache).

## Decision

No findings. Ship.
