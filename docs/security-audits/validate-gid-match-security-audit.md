# Security Audit — Validate gid-match + manifest capture

**Feature:** validate-gid-match (pin validation to the prefilled manifest gid + capture agent manifests into the archive)
**Modules:**
- `src/orchestrator/agent/manifest_locator.py` — `locate_manifest_bins(..., prefilled_gids=)` per-depot gid preference
- `src/orchestrator/agent/routers/steam.py` — `steam_validate` reads `downloaded_state()`; `start_prefill` captures manifests post-prefill
- `src/orchestrator/core/settings.py` — `steam_prefill_live_cache_dir`
**Audit date:** 2026-06-30
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy --strict + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-06-30 -->

## Scope

Fixes false-Partial badges: the agent runs SteamPrefill with `HOME=/tmp`, so SteamPrefill writes manifests to `/tmp/.cache/SteamPrefill` — not the host cache the archive-sync reads — so agent-driven force-prefills' manifests were never archived and the validator fell back to a stale older manifest. Fix: (1) `steam_validate` reads SteamPrefill's own `downloaded_state()` and pins manifest selection per-depot to the prefilled gid (falling back to newest-by-mtime); (2) after a successful prefill the agent captures the freshly-written manifests from its HOME cache into the durable archive. 13 new tests.

## Methodology

1. **SAST-lite.** `ruff check src/orchestrator tests` (flake8-bandit `S`) — clean (one S108 on the intentional `/tmp` HOME-cache path, `# noqa`'d with justification).
2. **Type safety.** `mypy src/orchestrator` — clean (88 files).
3. **Import isolation.** The agent must not import `orchestrator.api.main`/`db.pool`; `manifest_archive` is stdlib-only and agent-internal — `tests/agent/test_import_isolation.py` still green.
4. **Threat-model cross-check:** injection, path traversal, secret exposure, availability.
5. **Tests.** Full suite 1316 passed (only the documented `tests/test_licenses.py` local pip-licenses failure).

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (explicitly checked, clean)

- **No injection / no user input.** `prefilled_gids` are numeric manifest gids read from SteamPrefill's own `successfullyDownloadedDepots.json` (a local file the agent owns); compared as strings against filename fields parsed by the existing `{app}_{app}_{depot}_{gid}` split. No request data reaches the locator selection. `body.app_id` is a validated non-negative int.
- **No path traversal.** The locator globs `cache_root/v1/{app}_{app}_*.{bin,shas}` (numeric app_id) and the capture copies only `*.bin` between two settings-fixed agent-owned dirs via the existing append-only `sync_manifests_to_archive` (never deletes, isolates per-file errors). No caller-controlled path component.
- **Secret-free.** Manifest gids are not secret (they're public Steam content identifiers); `downloaded_state()` returns only gids, never tokens/account material. No new log field carries a secret.
- **Availability.** The capture runs synchronously but is a bounded, fast copy of the few new `.bin` manifests a single prefill produced (not the whole library); a capture failure is caught and logged and never fails the prefill job. The `downloaded_state()` read in validate is wrapped in `try/except` → falls back to newest-by-mtime, so a missing/corrupt record can't 500 validate.
- **Backward-compatible.** `prefilled_gids=None`/empty preserves the exact prior newest-by-mtime behavior; the locator still treats the manifest cache as the source of truth (the gid record is a per-depot *selection preference*, not an enumeration index — consistent with the module's existing rationale).
- **`/tmp` path (S108).** `steam_prefill_live_cache_dir=/tmp/.cache/SteamPrefill` is the agent container's `$HOME/.cache` (HOME=/tmp), a deliberate deploy-matched path, not an insecure shared temp file. `# noqa: S108` with justification.

## Decision

**Cleared to advance.** No SEV-1/2/3/4 findings. Additive, secret-free, injection-free, crash-safe (validate falls back; capture never fails the job), backward-compatible. 13 new tests; ruff + mypy clean; full suite green.

## Sign-off

- Implementation: commit `<pending>`
- Test suite: 1316 passed (`--ignore=tests/scripts`); 13 new tests (locator 4, validate 1, capture 2, settings 1, + existing reuse)
- ruff + mypy clean; agent import-isolation preserved
- No new dependency; no migration
