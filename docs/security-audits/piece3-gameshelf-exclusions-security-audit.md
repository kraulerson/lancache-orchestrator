# Security Audit — Piece 3 (orchestrator side): Game_shelf cross-launcher exclusions

**Feature:** gameshelf-exclusions — migration 0012 widens `prefill_exclusions.source` to
allow `'gameshelf'`, and a new reconcile endpoint
`PUT /api/v1/prefill-exclusions/gameshelf/{platform}` lets Game_shelf push the full set of
app_ids it considers already covered on a higher-priority launcher (e.g. an Epic copy also
owned on Steam). The orchestrator's Epic scheduled prefill (Piece 2) already skips any
`mode='exclude'` row, so a `gameshelf` exclude row suppresses the redundant Epic prefill.
**Modules:** `api/routers/prefill_exclusions.py`, `db/migrations/0012_prefill_exclusions_gameshelf_source.sql`
**Audit date:** 2026-07-04 · **Auditor:** self-review (Senior Security Engineer persona) + ruff (S) + mypy + full suite (1477) · **Phase:** 2, Build Loop 2.4

<!-- Last Updated: 2026-07-04 -->

## Methodology
ruff `--select S` clean, mypy clean (98 files), migration + router suites + full suite green
(1477 passed; only the pre-existing `test_licenses.py` tooling gap fails locally). Threat
cross-check: SQL injection, authorization/blast-radius, availability/DoS, data integrity,
migration data-loss.

## Findings
| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (checked, clean)
- **No SQL injection.** `platform` is validated against the static `_PLATFORMS` allowlist
  (→ 400) before use. `app_ids` are bound as `?` parameters through `execute_many` and the
  `DELETE … NOT IN (…)` clause, whose placeholder string is `?, ?, …` only — no caller data
  is ever interpolated into SQL text. The one dynamic-placeholder line carries `# noqa: S608`
  with the values passed as bound params (mirrors the existing `manifests.py` IN-clause
  pattern); ruff S-suite is otherwise clean.
- **Bounded blast radius (authorization).** The endpoint writes ONLY `source='gameshelf'`
  rows and deletes ONLY `source='gameshelf'` rows (and only for the given platform). It
  cannot read, modify, or delete `operator` or `classifier` overrides — proven by
  `test_does_not_clobber_operator_allow` and `test_does_not_delete_operator_or_classifier_rows`.
  A compromised Game_shelf token can therefore only add/remove gameshelf exclusions (suppress
  or re-enable Epic prefill for games) — within its intended authority; it cannot force a
  prefill, touch the cache, or override operator policy. Auth is the standard Bearer-token gate
  on all `/api/v1` routes (`test_no_token_401`).
- **Operator 'allow' stays sticky.** Insert uses `ON CONFLICT(platform, app_id) DO NOTHING`,
  so a game an operator has explicitly allowed is never flipped to excluded by a gameshelf push.
- **Availability / DoS.** `app_ids` is capped at 50 000 items and each id at 64 chars by the
  request model (`extra='forbid'`). The reconcile is a single bounded transaction
  (one `execute_many` insert + one delete). Malformed input (unknown platform, over-long id)
  is rejected at the edge as 400, never a 503 CHECK failure.
- **Atomicity / no half-state.** Insert + delete run inside one `write_transaction()`, so the
  gameshelf set is never left partially reconciled if a statement fails mid-way.
- **Migration is loss-less + tamper-pinned.** 0012 rebuilds the table via the standard
  rename-out → create-canonical → copy → drop-`_old` order (required by the migrate runner's
  post-apply sanity check, which tracks CREATE/DROP but not RENAME). All rows and every CHECK
  (platform, app_id length, mode, source, reason length) + `UNIQUE(platform, app_id)` survive —
  proven by `test_0012_rebuild_preserves_existing_rows`. The new checksum is pinned in
  `CHECKSUMS` (supply-chain defense: a migration file cannot be altered without a matching
  manifest edit that code review would catch).

## Decision
No findings. Ship. (Control-plane-only; deploy is a `git pull` + rebuild + recreate on LXC 1105 —
no agent change, no 2FA. The migration applies automatically at boot.)
