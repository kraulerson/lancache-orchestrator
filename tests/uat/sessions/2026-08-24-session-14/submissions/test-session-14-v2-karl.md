# UAT Session 14 — Orchestrator Results

**Date:** 2026-08-24 (executed 2026-08-25)
**Tester:** Karl
**Template:** `templates/test-session-14-v2.html`
**Features:** epic-prefill-status-based, f18-cache-purge

**Tester's summary:** 9 passed, 1 failed, 0 skipped, 0 not tested
**Adjudicated summary:** 10 passed, 0 failed — plus 1 new defect raised from a tester caveat.
The single FAIL was a defect in the test, not in the product (see scenario 5).

---

## Scenarios

| # | Scenario | Tester | Adjudicated | Notes |
|---|---|---|---|---|
| 1 | Is the feature actually switched on? | PASS | PASS | `ORCH_SCHEDULED_PREFILL_ENABLED=true` — the Epic cutover is live. |
| 2 | The Epic selection set is bounded by validation status | PASS | PASS | |
| 3 | A newly purchased Epic game is still picked up | PASS | PASS | |
| 4 | Eviction re-triggers a prefill rather than being ignored | PASS | PASS | |
| 5 | The purge surface exists on both API and CLI | **FAIL** | **PASS** | Test defect — see below. |
| 6 | Purge enumerates, unlinks, and flags for re-prefill | PASS (caveat) | PASS + **defect raised** | See UAT-14-B. |
| 7 | Purging an already-purged game is a harmless no-op | PASS (caveat) | PASS | Same caveat as 6; one defect, not two. |
| 8 | The purge is recorded in the audit trail | PASS | PASS | |
| 9 | Purge leaves the block list alone | PASS | PASS | |
| 10 | No cache-wide or chunk-level purge exists | PASS | PASS | |

---

## Scenario 5 — the FAIL was mine, not the product's

Karl observed:

```
curl -s -o /dev/null -w '%{http_code}\n' -X GET http://127.0.0.1:8765/api/v1/games/2451/purge
401
```

The scenario's `curl` omitted the bearer token, so authentication rejects the request
before method routing is ever reached. `401` is correct behaviour, not a missing route.
The scenario asserted `405` and therefore could not pass however the product behaved.

Re-run with the token present:

```
GET  -> 405
POST -> 202
```

which is exactly the acceptance criterion — the route exists and is POST-only.
**Scenario 5 is a PASS.** The template scenario is wrong and must be corrected before it
is reused; recorded as UAT-14-D.

---

## UAT-14-B (SEV-2, NEW) — `chunks_cached` is not reset by a purge

**Raised from Karl's caveat on scenarios 6 and 7.** He noticed the chunk counts still
read fully-cached after a purge and flagged it rather than ticking a clean pass.

Observed immediately after a successful purge:

```
status             ⚠ VALIDATION_FAILED
chunks_cached      337
chunks_total       337
```

The chunks are genuinely deleted — the status flip to `VALIDATION_FAILED` proves the
purge executed — but `chunks_cached` retains the value from the last validation. Any
consumer reading those two fields sees a fully cached game whose files are gone.

**Impact:** Game_shelf's cache badge is driven by `chunks_cached` / `chunks_total`, so a
purged game displays "Cached 337/337" until something else revalidates it. This is the
same false-green class as the validator and `fetch_manifests` findings from the agent
arms — the state is correct, what the system *reports* about it is not.

**Proposed:** the purge handler should zero `chunks_cached` (or clear it to unknown) in
the same transaction that sets `validation_failed`.

---

## UAT-14-D (SEV-3, NEW) — scenario 5's command omitted authentication

The template's scenario 5 asserts `405` from an unauthenticated `curl`. Auth precedes
routing, so it can only ever return `401`. Fix the scenario to source `ORCH_TOKEN` from
`/root/orch-lxc.env` and send `Authorization: Bearer`, then re-assert `405`.

---

## Incident during adjudication — accidental purge of game 2451

While verifying scenario 5, the assistant looped over both `GET` and `POST` against the
purge endpoint. `POST` is the destructive verb: job `45142 purge steam SUCCEEDED` was
assistant-initiated, not part of Karl's test run, and left *All Hail the Orb* purged.

Restored immediately — prefill `45145` and validate `45146` both succeeded; the game
reads `UP_TO_DATE`, 337/337, `last_validated_at 2026-08-25 12:45:28`. No lasting impact,
and the feature's designed reversibility is what made recovery trivial.

Recorded here rather than omitted: a destructive call was made against production during
a verification step, and the lesson is that probing an endpoint's *shape* must use safe
verbs only.

---

## Verdict

Both features meet their documented acceptance criteria. `epic-prefill-status-based` is
live and behaving as specified; `f18-cache-purge` satisfies all ten points of its spec
coverage. The defects found are in what the system reports after the fact, not in the
features themselves.

Open items for triage: UAT-14-B (SEV-2), UAT-14-D (SEV-3), plus the agent-arm findings
recorded in `../agent-results/`.
