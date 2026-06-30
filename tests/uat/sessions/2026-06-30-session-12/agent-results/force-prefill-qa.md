# Agent: Force-prefill QA hunt (UAT-12)

Confirmed against code. `_payload_force` is robust (NULL/non-JSON/non-dict → False);
payload is never user-controlled (bool seam only); `?force=1/yes/true` all parse;
auto-enqueued validate correctly carries no payload.

1. **SEV-3 — dedup force-upgrade UPDATE has no `state='queued'` guard → TOCTOU.**
   `api/routers/prefill_trigger.py` UPDATE keyed on id only; worker's `claim_next_job`
   (worker.py:55-67) flips queued→running and reads payload ONCE before the late UPDATE
   lands → prefill runs non-force yet DB records `force=true` and logs `force_upgraded`.
   → **FIXED** (added `AND state='queued'` + rowcount-aware logging).

2. **SEV-4 — Epic `?force=true` accepted+persisted but ignored.** `_epic_prefill` never
   calls `_payload_force`; CLI help implies it works everywhere. Harmless (Epic always
   re-downloads). → **Deferred.**

3. **SEV-4 — force onto a RUNNING prefill no-ops with a success-looking message.**
   Documented-as-accepted but operator gets no signal to re-run. → **Deferred.**
