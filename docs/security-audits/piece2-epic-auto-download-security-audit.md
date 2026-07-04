# Security Audit — Piece 2: Epic auto-download via the orchestrator

**Feature:** epic-auto-download — the orchestrator owns Epic prefill (EpicPrefill never auto-downloads new games). Epic `library_sync` added to the cron; `enqueue_scheduled_prefill` scoped to Epic; `enqueue_library_sync` parameterized by platform.
**Modules:** `scheduler/jobs.py`, `scheduler/manager.py`
**Audit date:** 2026-07-04 · **Auditor:** self-review + ruff (S) + mypy + full suite (1466) · **Phase:** 2, Build Loop 2.4

<!-- Last Updated: 2026-07-04 -->

## Methodology
ruff (S) clean, mypy clean (98 files), scheduler suite + full suite green. Threat cross-check: SQL injection, availability, WAN/DoS.

## Findings
| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (checked, clean)
- **No SQL injection.** `enqueue_library_sync`'s `platform` is bound as a `?` parameter and is only ever `'steam'`/`'epic'` from the scheduler registration (never request-derived). `enqueue_scheduled_prefill`'s `platform = 'epic'` is a static literal.
- **No new surface.** No new endpoints, request bodies, or external input. Two new scheduler jobs (epic library_sync, and the already-registered scheduled_prefill now Epic-scoped) run on the existing interval.
- **Availability.** Both callbacks keep the never-raises scheduler contract (try/except → return 0). The per-platform in-flight UNIQUE index dedups the epic library_sync exactly as it already does for steam.
- **WAN impact is intended + gated.** Enabling the Epic scheduled prefill (a deploy-time env flag) will prefill *uncached* Epic games — the whole point (EpicPrefill won't). It is still gated by `owned=1`, block_list, and `prefill_exclusions` (mode='exclude'), and dedups against in-flight prefills. Epic-scoping prevents double-prefilling every Steam game (SteamPrefill's job).

## Decision
No findings. Ship. (Deploy sets `ORCH_SCHEDULED_PREFILL_ENABLED=true` on the LXC and retires the host EpicPrefill cron.)
