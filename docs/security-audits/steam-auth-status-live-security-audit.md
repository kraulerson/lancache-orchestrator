# Security Audit — Steam auth_status live signal

**Feature:** steam-auth-status-live
**Module:** `src/orchestrator/api/routers/platforms.py` (`_live_steam_auth_status` + steam-row override in `list_platforms`)
**Audit date:** 2026-06-30
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy --strict + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-06-30 -->

## Scope

`GET /api/v1/platforms` (read by CLI `status` and the Game_shelf cache dashboard) reported Steam's `auth_status` from the `platforms.auth_status` DB column, which has had **no Steam writer since re-arch ③c** (the legacy ValvePython worker that wrote it was deleted), so it was frozen at a stale "expired". The fix overrides the **steam** row's `auth_status` with the live agent/driver signal `/health` already uses (`agent_client.auth_status()` when `agent_enabled`, else `prefill_driver.auth_status()` — each stats the persisted `account.config`), and clears the equally-stale `last_error`. Epic is unchanged (it has a real writer). 6 new tests.

## Methodology

1. **SAST-lite.** `ruff check src/orchestrator tests` (flake8-bandit `S`) — clean.
2. **Type safety.** `mypy src/orchestrator` — clean (88 files).
3. **Threat-model cross-check:** secret exposure, auth boundary, DoS/availability, information disclosure.
4. **Tests.** Full suite 1308 passed (only the documented `tests/test_licenses.py` local pip-licenses failure). 6 new tests cover override-ok, override-expired, no-signal fallback, live-check-error fallback, epic-unaffected, and the agent-enabled path.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (explicitly checked, clean)

- **No secret exposure.** The live check returns only an `ok`/`expired` boolean-derived string; `prefill_driver.auth_status()` / `agent_client.auth_status()` never return or log token bytes (they stat `account.config`). The override additionally **clears** the steam `last_error`, removing any stale operator string from the response rather than adding data.
- **Auth boundary unchanged.** `/api/v1/platforms` stays bearer-gated; no new endpoint or unauthenticated surface. `auth_status` (ok/expired) was already exposed by this endpoint — the change makes it *accurate*, not more revealing.
- **Availability / DoS.** The added agent call is wrapped so it **never raises** (`except Exception → None`), and on any failure (agent down, no client/driver, transport error) it falls back to the stored column value — a broken/unreachable agent cannot 500 or hang the platforms read beyond the agent client's own bounded timeout+retry (PR #207). No unbounded work; the call runs once per request against a fixed 2-row endpoint.
- **No injection / no new SQL.** No new query; the override is in-memory on the already-read row. Epic's row is never touched.
- **Defensive parsing.** `bool(st["ok"])` tolerates truthy/falsy; a malformed agent response that lacks `ok` raises `KeyError` → caught → `None` → DB fallback (no 500).
- **Information accuracy (positive).** Previously the column could only ever read "expired" for Steam (false negative) — now a genuinely-expired token will correctly read "expired" and a healthy one "ok", so the operator gets a truthful signal instead of a permanently-stale one.

## Decision

**Cleared to advance.** No SEV-1/2/3/4 findings. The change is additive, bearer-gated, secret-free, crash-safe (never raises, falls back to the stored value), and makes a previously-dead status field truthful. Covered by 6 new tests; ruff + mypy clean; full suite green.

## Sign-off

- Implementation: commit `<pending>` (appended after the green-phase commit lands)
- Test suite: 1308 passed (`--ignore=tests/scripts`); 6 new platforms tests
- ruff + mypy clean on `platforms.py`
- No new dependency; no migration
