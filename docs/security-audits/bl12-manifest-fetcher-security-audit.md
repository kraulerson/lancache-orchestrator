# Security Audit — BL12 Steam Manifest Fetcher

**Feature:** BL12-manifest-fetcher
**Audit date:** 2026-05-28
**Audited modules:**
- src/orchestrator/jobs/handlers/manifest_fetch.py
- src/orchestrator/api/routers/manifest_trigger.py
- src/orchestrator/platform/steam/worker.py (`_handle_manifest_fetch` IPC op)
- src/orchestrator/platform/steam/client.py (`manifest_fetch` method)
- src/orchestrator/db/migrations/0002_jobs_kind_manifest_fetch.sql

<!-- Last Updated: 2026-05-28 -->

## Scope

Post-implementation security review of the F1 milestone 3/3 manifest
fetcher: the `manifest_fetch` job handler, the worker CDN IPC op, the
operator trigger endpoint, and the `jobs.kind` CHECK-extension migration.

## Methodology

1. ruff / ruff format / mypy — all clean (47 source files)
2. gitleaks full working-tree scan — no leaks
3. semgrep --config=auto on all four source modules — 0 findings
4. Manual review against threat-model entries TM-005 (SQL injection),
   TM-009 (untrusted deserialization), TM-010 (auth bypass on
   write endpoints), TM-014 (resource pressure), and the ADR-0013
   worker-isolation boundary.

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

No new vulnerabilities introduced. One informational observation
(manifest-count, below) — not attacker-controlled in the threat model.

## Threat-model walk

- **TM-005 (SQL injection):** MITIGATED. Every statement in the handler
  and trigger router uses `?` placeholders. `game_id` arrives as a
  FastAPI `int` path parameter (non-int → 422 before any query). The
  upsert binds `str(gid)`, `int(chunk_count)`, `int(total_bytes)`, and
  the raw bytes as parameters — never string-interpolated. The
  `auth_status='expired'` update binds the truncated error string. The
  migration is static DDL with no runtime input.

- **TM-009 (untrusted deserialization):** MITIGATED — and this was the
  central design risk for BL12. The orchestrator NEVER deserializes the
  manifest BLOB (ADR-0013 D14). The handler base64-decodes the worker's
  `raw_b64` and stores the resulting bytes as an opaque BLOB; there is no
  `pickle.loads`, `eval`, or protobuf parse on the orchestrator side. The
  worker serializes via protobuf `.serialize()` (spike-A3 established that
  pickle is both unsafe and impossible here — `CDNDepotManifest` holds a
  `cdn_client` back-reference). The F7 validator will re-parse the BLOB
  inside the worker venv, where steam-next types already live.

- **TM-010 (auth bypass on write endpoints):** MITIGATED. The trigger
  endpoint is mounted under `/api/v1/games/...`, gated by
  `BearerAuthMiddleware`. `tests/api/test_manifest_trigger_router.py`
  asserts 401 for both missing and wrong bearer. The endpoint performs
  only the documented state transition (queue a job) — no parameter
  drives privileged behavior.

- **TM-014 (resource pressure):** MITIGATED for the realistic case. A
  single manifest BLOB exceeding `Settings.manifest_size_cap_bytes`
  (default 128 MiB) raises `ValueError` and fails the job rather than
  storing — guards a malformed/hostile CDN response. base64 decode
  happens before the size check, so a pathological `raw_b64` allocates
  its decoded form (~128 MiB ceiling) transiently before the guard
  trips; bounded by the worker's IPC message envelope upstream.

## Informational observation (non-blocking)

- **No cap on the *number* of manifest entries per fetch** (only per-BLOB
  size). A response with very many depot entries would write many rows
  and sum many `total_bytes`. Not treated as a SEV because the source is
  the operator's own authenticated Steam CDN for a single owned app —
  depot count is bounded by what Steam publishes for that appid, not by
  attacker input. If a future feature accepts third-party-supplied
  manifests, revisit with a per-fetch entry cap.

## Defensive-programming review

- **NotAuthenticated handling:** on a `SteamWorkerError` with
  `kind == "NotAuthenticated"` the handler flips
  `platforms.auth_status='expired'` (truncated `last_error[:200]`)
  before re-raising, so an expired session surfaces in `/platforms`
  (mirrors the BL11 F-UAT6-3 fix). The update is wrapped in its own
  try/except — a failure to mark expiry is logged, never masks the
  original auth error.
- **Malformed entries:** an entry missing any required field, or whose
  `raw_b64` fails base64 decode, is skipped with a WARN (not a crash).
  If every entry is invalid the handler exits without touching
  `games.size_bytes` (`upserted == 0` guard).
- **size_bytes only on success:** `games.size_bytes` is updated only
  when ≥1 manifest upserts, so a no-op/empty fetch never zeroes an
  existing size.
- **Migration integrity:** the 0002 recipe runs inside the migration
  runner's transaction with `PRAGMA foreign_keys=OFF` during the table
  swap (standard SQLite rebuild pattern), preserves all rows via the
  backup table, and recreates the four `idx_jobs_*` indexes identically
  to 0001. The CHECKSUMS manifest is pinned (supply-chain tamper
  detection); the rewritten file's checksum was regenerated.

## Operator surfaces (log events)

- `manifest_fetch.started` / `.returned` / `.upserted` (INFO) — job_id,
  game_id, app_id, counts. No credentials, no BLOB contents.
- `manifest_fetch.skipped_entry` / `.no_valid_entries` (WARN) — malformed
  CDN entries; logs depot_id and a truncated repr, no secrets.
- `manifest_fetch.session_expired_marked` (WARN) /
  `.session_expired_mark_failed` (ERROR) — auth-expiry bookkeeping.
- `manifest_trigger.queued` / `.dedup_hit` (INFO),
  `.db_unavailable` (ERROR), `.insert_invisible_after_write` (ERROR).

No PII, credentials, or manifest BLOB bytes appear in any log event.

## Sign-off

No SEV findings. BL12 cleared to ship.

— Senior Security Engineer persona, 2026-05-28
