# Security Audit — Piece 1: Steam auto-prune selectedAppsToPrefill.json

**Feature:** steam-prune-selection — the auto-classify-block actuator for Steam. The control plane hands the agent the Steam app_ids to remove ('exclude') / keep ('restore') and the agent reconciles SteamPrefill's `selectedAppsToPrefill.json` so the host prefill cron stops caching classifier-flagged non-games.
**Modules:**
- `platform/steam/selection_file.py` — pure `reconcile_selection` + `as_int`
- `agent/routers/steam.py` — `POST /v1/steam/prune-selection` (bearer-gated)
- `clients/agent_client.py` — `prune_steam_selection`
- `scheduler/jobs.py` — `enqueue_auto_classify_block` actuates via the agent client; `scheduler/manager.py`, `api/main.py` thread it through
**Audit date:** 2026-07-04
**Auditor:** self-review + ruff (flake8-bandit `S`) + mypy + full suite (1464)
**Phase:** 2 (Construction), Build Loop 2.4

<!-- Last Updated: 2026-07-04 -->

## Methodology
1. `ruff check` (S) — clean. 2. `mypy` — clean (98 files). 3. `tests/agent/test_import_isolation.py` green — `selection_file` is stdlib-only, so the agent still imports neither `api.main` nor `db.pool`. 4. Threat cross-check: path traversal, secrets, availability, destructive-safety.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (checked, clean)

- **No path traversal.** The target path is fixed: `settings.steam_prefill_config_dir / "selectedAppsToPrefill.json"`. The request body carries only integer app_ids (`list[int]`, Pydantic-validated); no request string reaches a filesystem path. The `.bak` sidecar is a fixed sibling name.
- **Non-destructive + reversible.** Only the app_id list is rewritten — never the cache. The ORIGINAL curated list is preserved once in `selectedAppsToPrefill.json.bak` (written only if absent). `restore` (operator 'allow') wins over `exclude`, so an un-excluded game is re-added, and a no-op change writes nothing.
- **Authz.** `/v1/steam/prune-selection` sits behind the agent bearer middleware, like every other `/v1/*` write. The control plane calls it over the same source-IP-allowlisted channel as prefill/validate.
- **No secrets.** No credential path; the endpoint reads/writes only the app_id list.
- **Availability.** The actuation in `enqueue_auto_classify_block` is wrapped in `try/except Exception` and never raises (scheduler-callback contract); a failed prune is logged and retried next tick. The DB `prefill_exclusions` rows are the source of truth, so a dropped prune loses no state.
- **Malformed input tolerated.** `reconcile_selection` drops non-integer / bool entries; an unreadable or non-list JSON file returns a no-op result instead of raising.

## Decision
No findings. Ship.
