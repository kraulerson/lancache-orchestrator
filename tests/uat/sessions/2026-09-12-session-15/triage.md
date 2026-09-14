# UAT Session 15 — Triage

**Date:** 2026-09-12
**Triaged with:** Karl (approved the disposition below verbatim)
**Bug tracker:** GitHub issues, this repo
**Outcome:** 10 issues, no SEV-1. 4x SEV-2, 4x SEV-3, 2x SEV-4.

| Issue | Sev | Title | Disposition |
|---|---|---|---|
| [#308](https://github.com/kraulerson/lancache-orchestrator/issues/308) | SEV-2 | One error history row pins a large game to the 300s budget | **Fix now (first)** |
| [#309](https://github.com/kraulerson/lancache-orchestrator/issues/309) | SEV-2 | `status_measured_at` surfaced in no interface | **Fix now (second)** |
| [#310](https://github.com/kraulerson/lancache-orchestrator/issues/310) | SEV-2 | Purge with all-failed unlinks writes cache truth, breaker-exempt | **Fix next** — blocked on a decision about partial-purge semantics |
| [#311](https://github.com/kraulerson/lancache-orchestrator/issues/311) | SEV-2 | No sweep can complete | **Fold into Option C** (approved pass-marker work) |
| [#312](https://github.com/kraulerson/lancache-orchestrator/issues/312) | SEV-3 | Nine writer-guard evasions | Defer to Phase 3 |
| [#313](https://github.com/kraulerson/lancache-orchestrator/issues/313) | SEV-3 | Failed Kuma push burns the dedupe stamp | Defer to Phase 3 |
| [#314](https://github.com/kraulerson/lancache-orchestrator/issues/314) | SEV-3 | Orphaned attempt-writes after cancellation | Defer to Phase 3 |
| [#315](https://github.com/kraulerson/lancache-orchestrator/issues/315) | SEV-3 | `VALIDATE_TIMEOUT_CEILING_SEC` not configurable | Defer to Phase 3 |
| [#316](https://github.com/kraulerson/lancache-orchestrator/issues/316) | SEV-4 | 19 games stuck at `status='failed'` | Defer — **operator judged the display acceptable in scenario 5** |
| [#317](https://github.com/kraulerson/lancache-orchestrator/issues/317) | SEV-4 | `record_job_outcome()` never fired in production | Defer — exercise deliberately in UAT 16 |

## Severity rules applied

Per CLAUDE.md: SEV-1 cannot be deferred; SEV-2 may be deferred during Phase 2 but must be
resolved or the feature removed at the Phase 2->3 gate. **No SEV-1 was found.** The four
SEV-2s are therefore all deferrable in principle; two are being fixed now anyway because
#308 is a one-line change guarding against silent data damage and #309 is the operator's
own finding.

## Not fixed, deliberately

**#316** — scenario 5 asked the operator directly whether the 19 unmeasurable games look
broken in Game_shelf. He judged them acceptable. That is a fix we now do not have to make,
and it is recorded so nobody re-opens it on aesthetics.

## Session scorecard

- Automated suite: 1866/1866 green, ruff and mypy --strict clean.
- Live systems: 6 of 7 pass; the failure is #311.
- Hands-on: 7 of 8 pass; the failure is #309.
- Best single evidence the release works: **17 Epic games would be queued for download
  right now under the old predicate and are correctly blocked.**
- PR #303 verified by hand on the genuinely largest game in the library
  (ARK: Survival Evolved, 377 GB, 369,317 chunks).

## Process debt from this session

Three broken commands reached the tester across two templates
(`python -m orchestrator.cli`, `orchestrator-cli jobs list`, and the wrong game named as
largest). `scripts/lint-uat-scenarios.sh` was never run; running it afterwards flagged 8
violations in v1 and 4 in v2. The authoring memory had already recorded the `jobs list`
defect from UAT-14. Recorded, not tracked as an issue — it is a process fault, not product.

Also stale and needing a docs pass: CLAUDE.md cites
`templates/uat/templates/test-session-template.html` (does not exist; the real path is
`tests/uat/templates/`) and states 1835 passing tests (actually 1863).
