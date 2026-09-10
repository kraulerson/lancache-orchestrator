# Security Audit — Validate timeout review remediation

**Feature:** validate-timeout-review-remediation (four substantiated findings from the adversarial review of PR #303)
**Modules:**
- `src/orchestrator/clients/agent_client.py` — ceiling bound documented, `validate_chunks_per_sec` constructor injection, dead `VALIDATE_TIMEOUT_PER_CHUNK_SEC` removed
- `src/orchestrator/core/settings.py` — `validate_assumed_chunks_per_sec` added; `sweep_batch_size` default 10 → 2
- `src/orchestrator/jobs/handlers/sweep.py` — candidates ordered least-recently-validated first
- `src/orchestrator/validator/disk_stat.py` — Steam passes its last known `chunks_total`
- `src/orchestrator/api/main.py` — Settings → AgentClient wiring
**Audit date:** 2026-09-01
**Auditor:** self-review (Senior Security Engineer persona) + adversarial reviewer (independent) + ruff (flake8-bandit `S`) + mypy + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-09-01 -->

## Scope

PR #303's own audit cleared it with one accepted SEV-4. An independent adversarial reviewer returned **BLOCK** with four substantiated findings, all verified in this session before any code was changed. Every one is the same shape as the defect #303 fixed — a number correct in isolation and wrong in the system around it.

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| 1 | SEV-2 | Ceiling was unbounded against `job_max_runtime_sec` | Fixed + test |
| 2 | SEV-2 | Per-call budget ignored the sweep's own concurrency | Fixed + test |
| 3 | SEV-2 | Steam never received a size-aware budget | Fixed + tests |
| 4 | SEV-3 | Tests did not constrain the implementation | Fixed + regression check |
| 5 | SEV-3 | `chunks_per_sec` was not reachable from config | Fixed + test |

### 1. Ceiling vs job budget (SEV-2, fixed)

A validate ceiling above `Settings.job_max_runtime_sec` (21600s) is a fiction: `worker.py:224` wraps every handler in `asyncio.wait_for`, so the job is cancelled first. **`CancelledError` is a `BaseException`**, so it bypasses the sweep's per-game `except Exception` isolation (`sweep.py:81`) — in-flight validates abort writing **no** `validation_history` row. That is precisely the "cannot self-correct" mechanism #303 was written to remove. 14400 ≤ 21600 already held; the defect was that nothing enforced it. Now pinned by a test that reads the real Settings value.

### 2. Sweep concurrency vs the agent's stat pool (SEV-2, fixed)

`sweep_batch_size` defaulted to 10 concurrent validates, all funnelling into the agent's `_CACHE_STAT_WORKERS = 2` executor (`disk_stat.py:55`) against a disk at 62–79% iowait. The 48–54 chunks/sec figure every budget derives from was measured **sequentially**, so under a sweep each call received roughly a fifth to a tenth of it while its budget assumed the full rate. Default lowered to 2 and pinned to `_CACHE_STAT_WORKERS` by a test so the two cannot drift apart again.

**Accepted cost:** any part of validate that is not stat-bound (Epic's base64 transfer and parse) loses overlap. Judged worth it — honest budgets over speculative concurrency on a saturated disk.

### 3. Steam had no size-aware budget (SEV-2, fixed)

`steam_validate` was called with no chunk count, keeping the flat 300s base — ~12,000–16,000 chunks at the measured rate. **PR #303's audit certified this as "backward-compatible: unaffected."** It is unchanged; on this hardware that means broken, and backward-compatibility was the wrong criterion to apply — the old behaviour was only ever safe on hardware that no longer exists. Steam self-enumerates agent-side so there is no manifest row to read, but the orchestrator holds the previous run's `validation_history.chunks_total`. A never-validated game has none and falls back to the base.

### 4. Tests did not constrain the implementation (SEV-3, fixed)

Demonstrated, not argued: setting the rate to `1.0` and the ceiling to `86400.0` — absurd in both directions, and a ceiling **above** the job budget — left all 28 tests passing. Reproduced in this session before writing any remediation. After the fix the same mutation **fails 2 tests**. This was the worst finding: #303's whole thesis is that the old constant hid an assumption nothing could flag, and its tests reproduced that property one layer up.

### 5. Config claim was false (SEV-3, fixed)

`validate_timeout_for`'s docstring claimed "overriding it must not require a code edit". As shipped there was no Settings field and both call sites used the module default, so the keyword was test-only surface wearing a config costume. Now a real `validate_assumed_chunks_per_sec` setting, injected via the AgentClient constructor at the composition root.

## Non-findings (explicitly checked, clean)

- **No new external input.** `validate_assumed_chunks_per_sec` is a pydantic-settings field with `gt=0`, sourced from env like every other setting; it is operator-controlled configuration, not request data. `chunk_count` for Steam comes from the orchestrator's own `validation_history`, never from a caller.
- **No injection surface.** The new query is parameterised (`WHERE game_id=?`) and selects one integer column.
- **Division still guarded.** `chunk_count > 0 and chunks_per_sec > 0` unchanged; `gt=0` on the setting makes a zero rate unconstructible.
- **Sweep ordering is not attacker-influenced.** `last_validated_at` is written only by the validator.
- **Lowering `sweep_batch_size` cannot starve.** The semaphore bounds concurrency, not total work; every candidate is still processed.
- **Test fakes fail loudly, deliberately.** The two existing `steam_validate` spies broke on the signature change — the spy doing its job. They now accept `chunk_count` **explicitly** rather than absorbing it through `**kwargs`, so the next drift breaks them too.
- **No secret exposure, no new log field, no filesystem or subprocess surface.**

## Decision

**Cleared to advance.** Three SEV-2s and two SEV-3s found by independent adversarial review, all fixed and each pinned by a test that fails against the pre-fix behaviour. Full suite 1806 passed, 3 deselected; ruff + mypy clean.

**Explicitly deferred:** Game_shelf's client-side poll ceilings (~90s on the per-game Validate button, ~12 min on the full-sweep page) will time out the UI on legitimately long validations. Cross-repo, needs its own change.

**Explicitly out of scope:** the storage regression. This makes the system correct on the hardware as it stands.

## Sign-off
