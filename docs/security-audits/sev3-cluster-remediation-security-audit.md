# Security Audit — SEV-3 cluster remediation (code review 2026-06-02)

**Date:** 2026-06-02
**Scope:** migration `0004_jobs_library_sync_unique.sql` (+ CHECKSUMS),
`scheduler/jobs.py`, `api/routers/sync.py`, `lancache/heartbeat.py`,
`core/logging.py`, `api/_query_helpers.py`, and their tests.
**Origin:** Five verified SEV-3 findings from the 2026-06-02 review, batched.
Persona: Senior Security Engineer — concrete exploits, not box-checking. The
batch was additionally re-reviewed by a 4-lens adversarial workflow whose
material findings are folded in below.

## Threat review by fix

| Fix | Vector | Assessment |
|-----|--------|------------|
| **Dedup migration 0004** | SQL injection in the cleanup `UPDATE` | No user input — static SQL with literal predicates. Parameterless. |
| | Data loss from the cleanup | Cancels only **duplicate** in-flight `library_sync` rows (keeps earliest per platform); terminal and other-kind/other-platform rows untouched (unit-verified). Migrations run at boot **before** the jobs worker starts, so a "running" duplicate is already an orphan the ID6 reaper would handle. One-time, best-effort, idempotent under the migration framework (applied-once via `schema_migrations`). |
| | Supply-chain | New migration is SHA-256-pinned in CHECKSUMS (computed the same way the runner verifies). |
| **enqueue / sync ON CONFLICT** | Duplicate-job DoS via concurrent triggers | Now DB-enforced: partial UNIQUE index guarantees ≤1 in-flight `library_sync` per platform; `ON CONFLICT DO NOTHING` collapses the race. The previous app-level SELECT-then-INSERT TOCTOU is closed. |
| | Wrong/leaked job_id from `/sync` | Returns the id of the single in-flight row (index-guaranteed unique). The narrow insert→select gap (deduped row completes) returns 503 for retry — no wrong id, no crash. |
| **Heartbeat `_force_refresh`** | Operator-forced refresh swallowed (stale health) | Flag cleared at refresh **start**, so an invalidate during an in-flight refresh still forces the next probe. No new external surface; `/health` only reads a boolean. |
| **Logging reserved-keys** | Log forgery / field spoofing | User kwargs for pipeline-owned keys (`correlation_id`, `level`, `timestamp`) are rescued to `user_<key>`, never overriding the authoritative value or being silently dropped. The two rescue loops are now mutually exclusive (a key cannot be double-processed even if `level`/`timestamp` were ever contextvars-bound). Rescued values still pass through `_redact_sensitive_values` (rescue runs earlier in the chain), so a sensitive value smuggled as `level=<secret>` is still redacted under `user_level`. |
| **`build_order_by_clause`** | ORDER BY injection via hand-built `SortField` | `allow_list` is now **required** (validates `field`) **and** `direction` is validated against `{asc, desc}`. Both interpolated components are now checked — the adversarial review caught that the original fix validated `field` but left `direction` (a `Literal` not enforced at runtime) interpolated raw. Not reachable via the routers (all use `parse_sort`), now closed at the helper regardless. |

## Findings

**0 open security findings.** One **SEV-1 (ORDER BY `direction` injection)** was
surfaced by the adversarial re-review of this batch and **fixed in the same
change** (validation + regression test). Everything else is net-positive: the
dedup race, the heartbeat staleness, and the log-field-clobber are all closed,
and no new external surface, input, or credential path is introduced.

## Residual / accepted

- Migration cleanup keeps the lowest-id in-flight row per platform regardless of
  state — accepted as best-effort one-time cleanup (migrations precede the
  worker; a cancelled duplicate `library_sync` is idempotently re-queued by the
  next cron tick). Unit-tested.
- `CREATE UNIQUE INDEX` has no `IF NOT EXISTS` — not required; the migration
  framework applies each migration exactly once.
