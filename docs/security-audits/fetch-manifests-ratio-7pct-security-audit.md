# Security Audit — Manifest-fetch failure alarm at 7%

**Feature:** fetch-manifests-ratio-7pct (lower the default of `fetch_manifests_max_failure_ratio` from 0.10 to 0.07)
**Modules:**
- `src/orchestrator/core/settings.py` — one numeric default and its comment
**Audit date:** 2026-10-08
**Auditor:** self-review (Senior Security Engineer persona) + ruff + mypy + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-10-08 -->

## Scope

Karl lowered the Kuma 176 alarm threshold from 10% to 7% after the first live run of the one-login Steam manifest helper (#361) measured 4.9% (59/1211, job 46856). The change is one `float` default (bounded `0.0 <= x <= 1.0` by the existing `Field`) plus tests and docs.

## Audit findings

None. No SEV-1 to SEV-4 findings.

## Non-findings (explicitly checked, clean)

- **No new input.** The value is a settings default, overridable only through the `ORCH_FETCH_MANIFESTS_MAX_FAILURE_RATIO` setting, which already existed and is read through the existing operator configuration channels (process environment, `.env`, and the `/run/secrets` directory; `settings.py:54-55`). None of those is a request, a header or a user-supplied file.
- **No auth, data or filesystem path touched.** `summarise_tally` is unchanged; it compares a ratio with the threshold.
- **Failure mode is fail-loud.** A lower threshold makes the heartbeat go DOWN sooner, never later, so the change cannot hide a failing run.
- **Bounds retained.** The `ge=0.0, le=1.0` validator is untouched.
- **Secret-free.** No new log field or value.

## Decision

Accepted as-is. No mitigations required.
