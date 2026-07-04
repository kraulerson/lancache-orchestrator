# Security Audit — #228 manifest fetcher transient-failure retry

**Feature:** issue-228-fetcher-transient-retry (classify transient DepotDownloader failures and retry with bounded exponential backoff so a Steam-rate-limited app recovers instead of being lost)
**Modules:**
- `src/orchestrator/platform/steam/manifest_fetcher.py` — `TransientFetchError`, `_TRANSIENT_RE`, `_run_with_retry`, transient classification in `_run_manifest_only`
- `src/orchestrator/core/settings.py` — `manifest_fetch_delay_sec` 3→8s; new `manifest_fetch_max_retries` (3), `manifest_fetch_retry_backoff_sec` (15.0)
- `src/orchestrator/agent/app.py` — passes the two new settings to both fetcher construction sites
**Audit date:** 2026-07-03
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-07-03 -->

## Scope

`fetch_manifests` failed ~every app with `rc=1` / "A task was canceled. Lost connection to Steam" when DepotDownloader logged on back-to-back — Steam rate-limits rapid logons (this is NOT expired auth; DD logs in clean). The fix classifies DD failures: a narrow stderr regex (`_TRANSIENT_RE`) plus a `subprocess.TimeoutExpired` catch mark rate-limit / lost-CM / hung-logon failures as `TransientFetchError` (a `RuntimeError` subclass); everything else stays a plain `RuntimeError`. `_run_with_retry` retries only transient failures, up to `max_retries`, sleeping `retry_backoff_sec * 2**attempt` capped at 120s. Permanent failures (app not owned, no build) raise immediately and are counted `failed` as before.

## Methodology

1. **SAST-lite.** `ruff check` (flake8-bandit `S`) on all three modules — clean.
2. **Type safety.** `mypy` on `manifest_fetcher.py` + `agent/app.py` — clean.
3. **Import isolation.** `tests/agent/test_import_isolation.py` still green — the fetcher remains stdlib+subprocess only, no `orchestrator.api.*` / `orchestrator.db.*` import.
4. **Threat-model cross-check:** availability / DoS (unbounded loop), ReDoS, secret handling, no new network surface.
5. **Tests.** `tests/platform/steam` + `tests/agent` (39) green; 4 new retry/classification tests.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (explicitly checked, clean)

- **Bounded retry — no infinite loop / self-DoS.** `_run_with_retry` stops after `self._max_retries` transient attempts (`if attempt >= self._max_retries: raise`), then the app is isolated and counted `failed` by `fetch_all`'s existing per-app `try/except`. Backoff is capped at `_MAX_BACKOFF_SEC = 120.0`, so a single persistently rate-limited app can add at most `max_retries` bounded sleeps — it can never wedge the sweep or spin the CPU. The delay increase (3→8s) and the backoff both *reduce* Steam logon pressure, not increase it.
- **No ReDoS.** `_TRANSIENT_RE` is a flat alternation of literal phrases with no nested quantifiers or overlapping ambiguity, matched against `proc.stderr` truncated by the caller's existing `stderr[:500]` logging. Linear-time; no catastrophic backtracking.
- **Secret handling unchanged.** No new credential path. `TransientFetchError` messages contain only the integer `app_id`. The existing `stderr[:500]` warning log is unchanged (DD rate-limit stderr is connection text, never the password/2FA/token — those are never on DD's stderr, and our argv still carries only `-username` + `-remember-password`, no `-password`).
- **No new input surface.** `app_id` still originates from the operator-curated selection list; the retry wrapper adds no caller-controlled data. `subprocess.run([...])` list form is unchanged — no `shell=True`, no interpolation.
- **No new network destination.** Retries re-invoke the same DepotDownloader → Steam CDN path already audited in the base fetcher; no new outbound category.

## Decision

No findings. Ship.
