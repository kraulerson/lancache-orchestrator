# UAT Session 15 Results — v1 submission

**Date:** 2026-09-12
**Tester:** Karl
**Features:** Cache validation integrity (#305), Validate timeout recalibration (#303)
**Summary:** 3 passed, 1 failed, 4 skipped, 0 not tested

| # | Scenario | Result | Notes |
|---|---|---|---|
| 1 | A cached game shows as cached, and says when it was measured | FAIL | Shows cached. No time on when the measurement was done. |
| 2 | Nothing is being auto-downloaded behind your back | PASS | |
| 3 | An unknown game triggers a check, never a download | SKIP | Only way to test this is to buy a new game. Not doing that. |
| 4 | Force a single validate and watch the status update | PASS | |
| 5 | The 19 permanently-stuck games are visible and harmless | SKIP | No explanation on how to test. |
| 6 | Badges and filters still agree after the September Game_shelf fixes | PASS | |
| 7 | The biggest game in the library validates without timing out | SKIP | No explanation of which game this is to test. |
| 8 | A known error case fails cleanly without poisoning anything | SKIP | No instructions on how to test. |

## Completeness assessment

**Scenario 1 FAIL is a genuine product finding**, not a test problem — see UAT15-B1.

**Scenarios 5, 7 and 8 were skipped because the template was under-specified.**
It named game ids (15035, 56) and a status value but gave no command to run and no
route through the UI. That is an authoring defect in v1, not tester error. They are
re-testable and are re-issued in v2.

**Scenario 3 is not testable as written.** It asked to observe an `unknown` game
being measured rather than downloaded, but reaching that state requires acquiring a
new game. Replaced in v2 with an equivalent read-only check against existing
`unknown` rows.
