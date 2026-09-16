# UAT Session 16 — triage

**Status: PROVISIONAL.** CLAUDE.md requires triage with the Orchestrator. Karl
delegated execution of the session ("Run the UAT yourself"), not the Fix
Now / Defer / Won't Fix decisions. Everything below is a recommendation awaiting
sign-off, except where a severity rule removes the choice.

| Issue | Sev | Title | Recommendation |
|---|---|---|---|
| #329 | SEV-2 | Measurement gate excludes 1351 of 3217 games from prefill | **Fix Now** — needs a design decision first |
| #332 | SEV-2 | Malformed agent purge response strands a false green | **Fix Now** |
| #330 | SEV-3 | Epic prefill monitor DOWN; silence conflates two states | Defer — blocked behind #329 |
| #331 | SEV-3 | Operator purge queues for hours with no feedback | Defer |
| #333 | SEV-3 | Three tests pass without proving their property | **Fix Now** — cheap, and they guard shipped code |
| #334 | SEV-3 | `get_settings()` could downgrade a breaker trip | Defer — latent, unreachable today |
| #312 | SEV-3 | Nine writer-guard evasions | Defer (pre-existing, phase-3) |
| #315 | SEV-3 | `VALIDATE_TIMEOUT_CEILING_SEC` not configurable | Defer (pre-existing, phase-3) |
| #316 | SEV-4 | 19 games stuck at `failed` | Blocked on Karl's EA-subgroup decision |
| #317 | SEV-4 | `record_job_outcome()` never exercised | Carry to session 17 |
| #326 | SEV-4 | Sweep monitoring sees liveness, not coverage | Defer — now has a real threshold (16.4h/pass) |

**No SEV-1.** Per CLAUDE.md, SEV-1 cannot be deferred; none was found.

## Reasoning on the two SEV-2s

**#329 is the most important thing found in this session**, and it is not a
defect in either feature under test — it was exposed by them. It cannot simply be
"fixed": the gate it violates was added deliberately in #305 to stop `unknown`
games triggering downloads on no evidence. Weakening it reopens that. The three
options are laid out in the issue; option 2 (make the games measurable via the
manifest-only fetcher) is the only one that does not weaken the rule, but its
coverage for this population is unverified. **Needs a decision before code.**

**#332 is the same failure mode as #310**, which we fixed eight days ago, reached
through unvalidated input rather than wrong counts. The real fix is the ordering
— a metadata parse failure must not suppress the cache-truth write — and that is
small and safe.

## Blocked scenarios

7, 8 and 9 (the live purge exercise) are blocked on job 46589, which is queued
behind the running sweep (#331). It will execute when the sweep ends. **The gate
should not be reset until those three are completed**, since the purge path is
still unexercised in production — which was one of the two reasons this session
existed.

## Recommendation on the gate

Do **not** reset the feature counter yet. Two of nine scenarios remain
unexecuted, and #329 arguably warrants its own remediation cycle before more
feature work begins.
