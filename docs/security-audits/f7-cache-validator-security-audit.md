# Security Audit — F7 Cache Validator

**Feature:** F7-cache-validator
**Audit date:** 2026-05-28
**Audited modules:**
- src/orchestrator/validator/cache_key.py
- src/orchestrator/validator/disk_stat.py
- src/orchestrator/validator/self_test.py
- src/orchestrator/jobs/handlers/validate.py
- src/orchestrator/api/routers/validate_trigger.py
- src/orchestrator/platform/steam/worker.py (`_handle_manifest_expand`)
- src/orchestrator/db/migrations/0003_manifests_depot_id.sql

<!-- Last Updated: 2026-05-28 -->

## Scope

Post-implementation review of the disk-stat cache validator: cache-key
derivation, the stat engine, the worker-side manifest expansion, the
validate job handler, the trigger endpoint, the startup self-test, and the
`manifests.depot_id` migration.

## Methodology

1. ruff / ruff format / mypy — all clean (52 source files)
2. gitleaks full working-tree scan — no leaks
3. semgrep --config=auto on `validator/`, `validate.py`, `validate_trigger.py`
   — 0 findings
4. Manual review against TM-005 (SQL injection), TM-008 (path traversal),
   TM-009 (untrusted deserialization), TM-010 (auth bypass on writes),
   TM-014 (resource pressure), and the ADR-0013 worker-isolation boundary.

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

No new vulnerabilities introduced.

## Threat-model walk

- **TM-008 (path traversal) — PRIMARY THREAT, MITIGATED.** The validator
  builds filesystem paths from `depot_id` and a chunk SHA, then `os.stat`s
  them. Both inputs are validated in `cache_key.steam_chunk_uri` before any
  path is built: `depot_id` must be a non-negative int and the SHA must
  match `^[0-9a-f]{40}$` (rejects `..`, `/`, NUL, uppercase, wrong length).
  The path itself is derived from an **md5 hex digest** (`cache_path`
  requires `^[0-9a-f]{32}$`), which structurally cannot contain traversal
  characters — the only path segments are 2-char hex dirs + the 32-char
  hex filename joined under `lancache_nginx_cache_path`. The SHA values
  originate from the operator's own manifest (parsed in the worker), not
  from request input. Defense in depth: even a malformed SHA that somehow
  reached `cache_path` would fail the hex regex.

- **TM-009 (untrusted deserialization) — MITIGATED.** Manifest protobuf
  parsing happens only in the worker venv (`_handle_manifest_expand`,
  ADR-0013 D14). The orchestrator receives only ints and 40-char hex
  strings over IPC; it never imports steam-next or calls protobuf/pickle.
  A malformed BLOB raises in the worker and is reported as a
  `ManifestParseError` IPC error, surfaced as job failure — no parser runs
  in the main process.

- **TM-005 (SQL injection) — MITIGATED.** Every query (latest-manifest
  selection, validation_history insert, games update, job dedup/insert)
  uses `?` placeholders. `game_id` is a FastAPI `int` path parameter.
  The migration is static DDL.

- **TM-010 (auth bypass) — MITIGATED.** The trigger endpoint is under the
  bearer-gated `/api/v1/games/...` prefix; tests assert 401 for missing and
  wrong tokens. It performs only the documented state transition (queue a
  `validate` job).

- **TM-014 (resource pressure) — MITIGATED.** Chunk counts are bounded by
  the operator's own manifests; SHAs are deduped per depot in the worker
  AND again orchestrator-side. `os.stat` calls are batched (256) and
  offloaded via `run_in_executor` so the event loop is never blocked.
  No attacker-controlled amplification.

## Defensive-programming review

- **`error` outcome never clobbers state:** a cache-mount failure or
  missing manifests records `outcome='error'` in `validation_history` but
  leaves `games.status` untouched (`_STATUS_FOR` has no `error` key) — an
  infra blip cannot mislabel a game as `validation_failed`.
- **Presence = exists AND size>0, never size-match:** cache files carry an
  nginx cache-entry header, so they are larger than the chunk body; the
  validator deliberately does not compare sizes (spike A4). A per-file
  `os.stat` `OSError` (e.g. EACCES) counts that path as missing rather than
  aborting the whole run.
- **Self-test gating:** `validator_self_test` requires the cache root to be
  a listable directory; failure sets `validator_healthy=False` →
  `/health` 503, so a misconfigured mount is visible before validations are
  trusted. A cache *miss* during validation does not flip health (only a
  mount/derivation error does).
- **Offline expansion:** `manifest.expand` needs no Steam session, so
  validation works with expired auth and never triggers a credentialed
  network call.
- **Migration 0003:** nullable `ADD COLUMN` (STRICT-safe, no rebuild),
  CHECKSUMS regenerated (supply-chain pin). `depot_id IS NOT NULL` guard in
  the latest-per-depot query ignores any legacy null rows.

## Operator surfaces (log events)

- `validate.started` / `validate.stat_done` / `validate.recorded` (INFO) —
  job_id, game_id, counts, outcome. No secrets, no chunk bytes, no paths.
- `validator.self_test.ok` / `.cache_root_missing` / `.failed` —
  diagnostic, path only.
- `validate_trigger.queued` / `.dedup_hit` / `.db_unavailable` /
  `.insert_invisible_after_write`.

No PII, credentials, or BLOB contents appear in any log event.

## Sign-off

No SEV findings. F7 cleared to ship. Live end-to-end validation (real
deserialized manifest vs. real cache) is a UAT step — it requires the
operator's Steam 2FA to first fetch manifests; the cache-key formula
itself is already verified against the live cache (spike A4).

— Senior Security Engineer persona, 2026-05-28
