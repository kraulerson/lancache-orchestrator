# Security Audit — Validate timeout recalibration

**Feature:** validate-timeout-recalibration (express the assumed disk throughput explicitly; raise the ceiling above the largest real game)
**Modules:**
- `src/orchestrator/clients/agent_client.py` — `VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC`, `VALIDATE_TIMEOUT_CEILING_SEC`, `validate_timeout_for(..., chunks_per_sec=)`
**Audit date:** 2026-09-01
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-09-01 -->

## Scope

The OMV rebuild of the NAS on 2026-08-31 removed the `ugacl` shim and left the NVMe cache that fronted the RAID0 detached. Validation throughput fell from a measured 1,471–1,539 chunks/sec to 48–54. The per-chunk timeout constant encoded an unstated assumption of ~461 chunks/sec, so every game above ~16,826 chunks — **379 of 1,811 (21%)** — could no longer finish inside its own budget, and because a timed-out validate writes no `validation_history` row, those games could never self-correct.

Change: the rate becomes an explicit assumed-throughput constant (40.0, deliberately below the measured 48) plus a `chunks_per_sec` keyword override; the ceiling rises 1,800s → 14,400s. 18 new tests; one superseded test replaced with the constraint that still holds.

## Methodology

1. **SAST-lite.** `ruff check src/orchestrator/clients/agent_client.py tests/clients` — clean.
2. **Type safety.** `mypy src/orchestrator/clients/agent_client.py` — clean.
3. **Threat-model cross-check:** injection, path traversal, secret exposure, availability, resource exhaustion.
4. **Tests.** Full suite 1796 passed, 3 deselected.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| 1 | SEV-4 (accepted) | A wedged agent can now hold a worker slot for 4h instead of 30m | Accepted — see below |

### 1. Longer ceiling extends the worst-case hold on a worker slot (SEV-4, accepted)

Raising `VALIDATE_TIMEOUT_CEILING_SEC` from 1,800s to 14,400s means a genuinely wedged or unreachable agent can occupy the validate path for four hours rather than thirty minutes before the client gives up.

**Why it is accepted rather than mitigated.** The bound still exists and is still finite, which is the property that matters — the ceiling's purpose is to stop an *unbounded* hang, not to schedule work. The alternative is strictly worse and was the live defect: a ceiling below the largest game's genuine requirement means 21% of the library can never validate and never self-corrects, which is a permanent correctness failure rather than a temporary availability one. Pacing belongs to the sweep, not to a client timeout constant.

**Residual exposure is bounded by design:** validate is read-only disk-stat, it holds no lock and mutates nothing until it returns, the orchestrator job's own timeout remains the primary guard, and a stuck job is cleaned by the startup reaper (observed reaping 1 orphan on 2026-09-01). No unauthenticated caller can reach this path — `/api/v1/games/{id}/validate` requires the bearer token, and the sweep is scheduler-driven.

## Non-findings (explicitly checked, clean)

- **No new external input.** `chunks_per_sec` is a keyword-only parameter with a module-constant default. It is not read from a request, a header, or a config file in this change; both call sites (`agent_client.py:249`, `:274`) use the default. No attacker-reachable path sets it.
- **Division guarded.** `chunk_count / chunks_per_sec` is reached only under `chunk_count is not None and chunk_count > 0 and chunks_per_sec > 0`. A zero or negative throughput falls back to the base budget rather than raising or producing a negative/infinite timeout. Tested (`test_a_nonsense_count_falls_back_to_the_base`).
- **No injection, no path traversal, no filesystem or subprocess surface.** The change is arithmetic over an integer already held in `manifests.chunk_count`.
- **Secret-free.** No new log field, no new value in an error path. Timeout values are not sensitive.
- **Connect timeout unchanged at 10s.** Only the READ budget scales — reaching the agent is fast or it is broken, so a network-level hang is still caught in 10s regardless of game size. Explicitly tested.
- **Backward-compatible.** `validate_timeout_for(None)` and nonsensical counts return exactly the prior base budget, so steam (which self-enumerates agent-side and passes no count) is unaffected.
- **No denial-of-service amplification.** The budget scales linearly with a value the orchestrator itself holds, capped. A caller cannot inflate it: `chunk_count` comes from the manifest record, not from the request.

## Decision

**Cleared to advance.** One accepted SEV-4 with the trade-off stated and bounded; no SEV-1/2/3. Additive and backward-compatible, no new input surface, division guarded, ruff + mypy clean, full suite green (1796 passed).

**Explicitly out of scope:** the storage regression itself. This change makes the system correct on the hardware as it currently stands; restoring a cache layer is an infrastructure decision tracked separately.

## Sign-off
