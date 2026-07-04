# Security Audit — Epic scheduled_prefill status-based enqueue (go-live fix)

**Feature:** epic-prefill-status-based — `enqueue_scheduled_prefill` keys the Epic prefill
decision off validation status (`status <> 'up_to_date'`) instead of the cached/current
version-diff. Epic has no version data (the Epic library API returns no buildVersion), so the
version-diff could never be cleared and re-enqueued the whole Epic library every tick.
**Modules:** `scheduler/jobs.py` (one `WHERE` clause + docstring)
**Audit date:** 2026-07-04 · **Auditor:** self-review + ruff (S) + mypy + full suite (1509) · **Phase:** 2, Build Loop 2.4

<!-- Last Updated: 2026-07-04 -->

## Methodology
ruff (S) clean, mypy clean, scheduler suite + full suite green (1509; only the pre-existing
`test_licenses` tooling gap fails). Threat cross-check: SQL injection, availability, WAN/DoS.

## Findings
| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (checked, clean)
- **No SQL injection / no new surface.** The change is a static literal `WHERE` predicate
  (`g.status <> 'up_to_date'`) replacing another static predicate. No new input, endpoint, or
  parameter. The callback keeps its never-raises contract.
- **Availability (the point of the fix).** The old version-diff enqueued the ENTIRE non-excluded
  Epic library (~532) every scheduler tick and the prefill could never clear it — an unbounded
  self-perpetuating job flood on the steal-bound agent. The fix bounds enqueues to games not
  validated as cached (~19 today), and a prefill→validate→`up_to_date` removes a game from the
  set, so steady-state is only genuinely-uncached games. `ON CONFLICT DO NOTHING` + the in-flight
  UNIQUE index still dedup a game already queued/running (so a `downloading`-status game can't
  double-enqueue), and the block_list / `prefill_exclusions` guards are unchanged.
- **WAN impact reduced, not increased.** Fewer prefills enqueued than before; the whole reason
  for the fix is to stop a redundant/looping prefill wave.

## Decision
No findings. Ship. (Control-plane-only; unblocks the Epic cutover — after deploy, enabling
`ORCH_SCHEDULED_PREFILL_ENABLED=true` enqueues only the ~19 not-`up_to_date` Epic games.)
