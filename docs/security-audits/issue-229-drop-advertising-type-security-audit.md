# Security Audit — #229 follow-up: drop `advertising` from exclude types

**Feature:** issue-229-drop-advertising-type
**Module:** `src/orchestrator/platform/steam/selection_classifier.py` — remove `"advertising"` from `_NON_GAME_TYPES`
**Audit date:** 2026-07-04
**Auditor:** self-review + ruff + mypy + suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-07-04 -->

## Scope

The live `selection classify` run flagged real games (Darksiders II 50650, Eufloria 41210) because Steam types some real games' app_ids as `advertising`. Dropping `advertising` from the exclude set removes those false positives. Behavior-only change to a read-only, advisory classifier; no new inputs, I/O, or surface.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings

- **Strictly reduces flagging.** Removing a member from the exclude frozenset can only make `classify()` return `None` for more inputs — it never flags anything new and never widens any surface. The full parent-feature audit (`issue-229-selection-classifier-security-audit.md`) still applies: read-only, no ReDoS, the selection is never mutated.

## Decision

No findings. Ship.
