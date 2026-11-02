# UAT Session 17 Results

**Date:** 2026-09-22
**Tester:** Unknown
**Features:** keys_zone Alarm, Retire failed status

**Summary:** 10 passed, 0 failed, 0 skipped, 0 not tested

---

## Scenarios

| # | Scenario | Result | Notes |
|---|---|---|---|
| 1 | The cache-index gauge reports a number and says what limits it | PASS |  |
| 2 | The gauge updates itself once a day without being asked | PASS |  |
| 3 | The watchdog reports in every 15 minutes | PASS |  |
| 4 | Cache loss is a SEPARATE indicator from the watchdog | PASS |  |
| 5 | A real cache-loss alarm reaches your phone | PASS |  |
| 6 | All three indicators are wired to send notifications | PASS |  |
| 7 | REQUIRES DEPLOY - the 19 permanently-failed games now read Blocked | PASS |  |
| 8 | REQUIRES DEPLOY - nothing ELSE changed status | PASS |  |
| 9 | A failed download no longer corrupts the cache record | PASS | It's blocked. No fail.  |
| 10 | Your judgement - would these indicators actually tell you something is wrong? | PASS |  |
