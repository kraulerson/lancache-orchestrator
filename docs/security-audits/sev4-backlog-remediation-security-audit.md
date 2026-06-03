# Security Audit — SEV-4 backlog remediation (code review 2026-06-02)

**Date:** 2026-06-02
**Scope:** `db/pool.py` (`health_check`, `acquire_writer`, `_checkout_reader`,
`__init__`), `core/settings.py` (`pool_busy_timeout_ms`, token-error scrub),
`jobs/handlers/manifest_fetch.py` (temp-file cleanup), and their tests.
**Origin:** The five SEV-4 findings from the 2026-06-02 review, batched. Persona:
Senior Security Engineer. The batch was re-reviewed by a 3-lens adversarial
workflow whose material findings are folded in below — one of which was a
**SEV-1 regression introduced by the first cut of the token-redaction fix**.

## Threat review by fix

| Fix | Vector | Assessment |
|-----|--------|------------|
| **health_check idle-only probe** | False `/health` 503 (availability) | A busy reader no longer trips a false-unhealthy / 503. In-use readers are tracked by an explicit `_inuse_readers` id-set maintained in `_checkout_reader` (no `asyncio.Queue` internals). A reader racing idle→checked-out between the in-use check and the probe is an accepted, vanishingly-rare residual that self-heals on the next probe (cached). Counting an in-use reader healthy is the safe direction; a genuinely broken reader is replaced by the read path. |
| **acquire_writer rollback** | Cross-caller write corruption | A forgotten transaction on the raw escape hatch no longer bleeds uncommitted writes into the next writer's commit — it is rolled back on exit (best-effort, and now logged at WARN so the misuse is visible). No new surface; the method was already a tested, documented escape hatch. |
| **pool_busy_timeout_ms ≥ 100** | DoS via write-conflict storm | Disallowing `0` prevents an operator from disabling the busy wait (which would surface every momentary lock as `WriteConflictError`). Audited deploy configs — none set a sub-100 value, so no breaking change. |
| **token-error scrub** | **Secret disclosure (raw token → logs)** | See the SEV-1 below. Final state: each error-`loc` element is matched exactly + case-insensitively against the field's lookup names (`orchestrator_token`, `orch_token`), covering the env alias path. Verified empirically that a too-short `ORCH_TOKEN` now raises a scrubbed `ValueError` with the candidate absent. |
| **manifest_fetch temp cleanup** | Disk exhaustion (temp-file leak) | A size-cap raise or skipped malformed entry no longer leaks the worker's pre-written depot temp files; an outer `try/finally` unlinks all `raw_path`s (idempotent). No untrusted-path handling changed (paths come from the trusted worker). |

## Findings

**1 SEV-1 — caught in adversarial review and fixed in-batch:** the first cut
replaced the `"token"` substring with an exact match on the field name only
(`{orchestrator_token}`). Pydantic places the matched **alias** in the error
`loc`, so a too-short token supplied via `ORCH_TOKEN` (the production env var)
has `loc=('ORCH_TOKEN',)` and would have **fallen through unscrubbed**, echoing
the raw candidate token in the propagated `ValidationError.input_value` — a
reintroduction of the exact SEV-2 leak the scrub exists to prevent (Bible §7.3),
and worse than the substring it replaced. Fixed by matching all lookup names
case-insensitively; added a regression test exercising the env-alias path;
confirmed empirically.

**0 other open findings.** The remaining fixes are net-positive: a false-503
removed, a cross-caller write-corruption hazard closed, a config footgun gated,
and a temp-file leak eliminated — no new external surface.

## Residual / accepted

- health_check retains a vanishing idle→checked-out race window (inherent to any
  snapshot-then-probe); documented in-code, self-heals on the next `/health`.
- `acquire_writer` swallows a rollback exception (best-effort) but now logs the
  rollback at WARN; the body's own exception is never masked (rollback runs in a
  `finally` with `contextlib.suppress`).
- `_SECRET_FIELD_NAMES` must be kept in sync with the `orchestrator_token`
  `AliasChoices`; noted in-code next to both.
