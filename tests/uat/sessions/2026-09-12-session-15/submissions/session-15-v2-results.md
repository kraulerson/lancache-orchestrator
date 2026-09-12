# UAT Session 15 Results — v2 submission

**Date:** 2026-09-12
**Tester:** Karl
**Summary as submitted:** 3 passed, 1 failed, 0 skipped

| # | Scenario | Result | Resolution |
|---|---|---|---|
| 3 | An unknown game is waiting to be measured, not queued for download | FAIL → **PASS on re-run** | Instruction defect, not a product defect. See below. |
| 5 | The 19 permanently-stuck games look explainable, not alarming | PASS | Operator judged the display acceptable — no UI change needed. |
| 7 | The genuinely largest game validates without timing out | PASS | ARK: Survival Evolved (1242, 377 GB, 369,317 chunks) completed. #303 verified by hand. |
| 8 | A game with no manifest fails fast and stays honest | PASS | Game 56 errored quickly both times, status unchanged. |

## Scenario 3 — resolved, two instruction faults

Two failures, neither in the product:

1. **`orchestrator-cli jobs list` is not valid.** The correct form is
   `orchestrator-cli jobs --kind prefill [--state ...] [--limit N]`. This is the third
   unverified command this session's templates handed the tester. Cause: I verified
   `game show` and `game validate` against the live system but assumed the `jobs`
   subcommand shape instead of running `--help`.
2. **`36057` was typed for `360757`** — a six-digit id entered as five, returning
   `HTTP 404: game not found`. An easy slip; the template should have led with the title.

**Substance verified directly instead:**

```
game show 360757  -> Steelrising,    status • UNKNOWN, last_validated_at None, last_error None
game show 485801  -> Astral Ascent,  status • UNKNOWN, last_validated_at None, last_error None
jobs --kind prefill --limit 20 -> 13 rows, ALL "⊘ CANCELLED  cancelled 2026-09-01: queued b..."
```

Every prefill job in the entire history belongs to the cancelled 2026-09-01 batch.
There are none since. Neither unknown game has a prefill job. The property holds:
`unknown` means "not yet measured", never "go download it".

Recorded as PASS with the caveat that the operator did not personally complete it.

## Session outcome

All 8 scenarios now accounted for: **7 pass, 1 genuine failure (UAT15-B1).**

Human testing confirmed by hand what the automated agents could not judge:
- #303 works on the real largest game in the library (scenario 7).
- The 19 unmeasurable games are not misleading in the UI (scenario 5) — no fix needed.
- The September Game_shelf filter fixes still hold (v1 scenario 6).
- Nothing is auto-downloading (v1 scenario 2, and re-confirmed above).
