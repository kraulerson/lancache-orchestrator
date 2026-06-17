# F8 — Block List + Scheduled Prefill Driver — Security Audit

**Date:** 2026-06-17
**Feature:** F8 (block list + version-diff scheduled prefill driver)
**Auditor:** AI agent (Senior Security Engineer persona) + adversarial-review subagent
**Branch:** `feat/f8-block-list`

## Scope

New/changed surface: `block_list` REST router (GET/POST/DELETE), `games.blocked`
flag, the `enqueue_scheduled_prefill` diff, `current_version`/`cached_version`
population, the steam prefill manifest-refresh, and the CLI `game block/unblock`.

## Method

TDD throughout, then a full gate sweep (pytest 1253, ruff, `mypy --strict`,
semgrep `orchestrator-rules.yaml` = 0 findings, gitleaks = no leaks) plus a
dedicated adversarial-review subagent instructed to *refute* — attacking SQL
injection, the diff's NULL-safety, the POST race, over-blocking, auth, and the
`cached_version` invariant.

## Findings & disposition

| ID | Sev | Finding | Disposition |
|----|-----|---------|-------------|
| S2-1 | SEV-2 | Epic prefill never wrote `cached_version` → every owned Epic game re-prefilled on every 6h tick forever (the `IS NULL` diff arm stays true). | **Fixed** — Epic success now stamps `cached_version=current_version` (Epic always fetches a fresh manifest, so it's accurate). Regression test added. |
| S2-2 | SEV-2 | Steam prefill reused stale manifests; a *patched* game would download old chunks yet get stamped `cached_version=current_version`, so the patch was silently never prefilled. | **Fixed** — steam prefill now re-fetches a fresh manifest when version-diverged (`cached_version != current_version`); `cached_version` is the **sole** responsibility of prefill (validate no longer writes it, since a standalone sweep can validate a stale manifest). Tests cover diverged-refetch + up-to-date-reuse + validate-never-writes. |
| S4-1 | SEV-4 | CLI `unblock` did not URL-encode `app_id`; an Epic appName with `/` would mis-route the DELETE. | **Fixed** — `quote(app_id, safe="")` on the path segment. Test added. |
| S3-1 | SEV-3 | A `current_version IS NULL` + `cached_version` set row is silently skipped by the diff with no diagnostic. | **Accepted (low risk)** — `library_sync`'s `COALESCE` preserves a prior non-null `current_version`, so this requires a never-versioned-yet-cached row (not reachable via the normal pipeline). Noted as a known limitation; safe direction (never blind-prefills). |

## Cleared (no defect) — adversarially verified

- **SQL injection / column ambiguity:** `block_list` GET and `games.blocked`
  build WHERE/ORDER only from allow-list-validated field names; all user values
  bind through `?` placeholders. `games.blocked` uses a correlated `EXISTS`
  subquery (alias `b`) — the outer clauses reference bare `games.*` columns, no
  ambiguity. POST/DELETE/INSERT/SELECT all parameterized. semgrep no-f-string-sql
  passes (static `_COLUMNS` interpolation only, with `# noqa: S608`).
- **POST 201-vs-200 race:** `execute_write` serializes on the single writer
  connection; the first INSERT gets `rowcount=1` → 201, a concurrent duplicate
  hits `ON CONFLICT DO NOTHING` → `rowcount=0` → 200. No false 201s.
- **Over-blocking:** `block_list` is consulted only by `enqueue_scheduled_prefill`
  (skip) and the `games.blocked` display flag. Manual `POST /games/{id}/prefill`
  and `/validate`, and the F13 sweep, never reference it — exactly per spec.
- **Input bounds:** `app_id` ≤64, `reason` ≤500, `platform`/`source` are pydantic
  `Literal`s — enforced at the API layer AND by the table CHECK constraints.
- **Auth:** all three block-list endpoints are under `/api/v1` and not in
  `AUTH_EXEMPT_PATHS`; the global bearer middleware gates them (verified by
  `test_post_requires_auth_401`).
- **DoS / idempotency:** duplicate-block floods are O(1) no-ops (`ON CONFLICT`);
  the scheduled enqueue is bounded by library size and deduped by the
  migration-0006 in-flight index.

## Conclusion

No open findings above SEV-3. The two SEV-2 correctness defects surfaced by the
adversarial pass were fixed test-first and re-verified. No secrets, no injection,
no auth gaps.
