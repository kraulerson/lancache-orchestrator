# Security Audit — F5 Steam Prefill

**Feature:** F5-steam-prefill
**Audit date:** 2026-05-29
**Audited modules:**
- src/orchestrator/prefill/downloader.py
- src/orchestrator/jobs/handlers/prefill.py
- src/orchestrator/api/routers/prefill_trigger.py
- src/orchestrator/validator/disk_stat.py (readability change)
- src/orchestrator/core/settings.py (prefill fields)

<!-- Last Updated: 2026-05-29 -->

## Scope

Post-implementation review of the prefill download path: the async httpx
chunk downloader, the prefill job handler (+ ID5 auto-validate), the trigger
endpoint, and the bundled F7 readability change.

## Methodology

1. ruff / ruff format / mypy — all clean (56 source files)
2. gitleaks full working-tree scan — no leaks
3. semgrep --config=auto on prefill/, prefill.py, prefill_trigger.py — 0 findings
4. Manual review against TM-005 (SQL injection), **TM-011 (SSRF / outbound
   request forgery — primary for a downloader)**, TM-010 (auth bypass on
   writes), TM-014 (resource pressure).

## Findings

**SEV-1: 0   SEV-2: 0   SEV-3: 0   SEV-4: 0**

No new vulnerabilities introduced.

## Threat-model walk

- **TM-011 (SSRF) — PRIMARY, MITIGATED.** The download URL is
  `{lancache_base_url}{uri}` where `uri = /depot/{depot_id}/chunk/{sha}`.
  `depot_id` is an int and `sha` is validated `^[0-9a-f]{40}$` by
  `validator.cache_key.steam_chunk_uri` (reused) before any URL is built —
  no path traversal, no scheme/host injection. The host/scheme come only from
  `lancache_base_url` (operator config, not request input). The `Host` header
  is the fixed `steam_cdn_host` setting. So a malicious manifest cannot make
  the orchestrator request an arbitrary host — at worst it yields more
  `/depot/.../chunk/...` paths against the configured lancache. depot_id/sha
  originate from the operator's own manifests (parsed in the worker), not from
  API input.
- **TM-005 (SQL injection):** MITIGATED. All handler/trigger queries use `?`
  placeholders; `game_id` is a FastAPI `int` path param; the ID5 validate
  enqueue uses literal column values.
- **TM-010 (auth bypass):** MITIGATED. The trigger is under the bearer-gated
  `/api/v1/games/...` prefix (tests assert 401 missing/wrong token). It only
  queues a job.
- **TM-014 (resource pressure):** MITIGATED. Concurrency is bounded by
  `Semaphore(chunk_concurrency=32)`; each chunk has a read timeout
  (`prefill_chunk_timeout_sec`) and bounded retries
  (`prefill_chunk_max_attempts`, [1,4,16]s backoff). Bodies are streamed and
  discarded (`aiter_bytes`, no full-buffer) → constant memory regardless of
  chunk size. Chunk count is bounded by the operator's own game. One game at a
  time (single jobs worker loop). `/health` stays responsive (all async I/O).

## Defensive-programming review

- **`error` / failure handling:** any failed chunk (after retries) → job
  failed + `games.status='failed'` + summarized error (failure list capped at
  50). No validate is enqueued on failure. 4xx is not retried (won't be fixed
  by retry); 5xx/timeout/transport-error are.
- **ID5 enqueue** uses `source='scheduler'` (a CHECK-allowed value) — verified
  against the `jobs.source` constraint.
- **F7 readability change** uses `stat()` only (`st_mode & 0o400`), no
  `open()` — adds no new file-read surface, and correctly excludes mode-000
  files the orchestrator (uid 1000) couldn't read anyway.
- **Stream lifetime:** the single `AsyncClient` is created/closed via
  `async with`; per-chunk `client.stream(...)` is also context-managed, so
  connections are released even on error/retry.
- No secrets in URLs or logs; chunk URLs are unauthenticated content paths.
  Failure reasons are HTTP status / exception-type strings only.

## Operator surfaces (log events)

- `prefill.started` / `prefill.fetching_manifests` / `prefill.completed`
  (total/ok/failed) / `prefill.validate_enqueued` (INFO).
- `prefill_trigger.queued` / `.dedup_hit` / `.db_unavailable` /
  `.insert_invisible_after_write`.

No PII, credentials, or chunk bytes appear in any log event.

## Sign-off

No SEV findings. F5 cleared to ship. The MISS→upstream→cache path (a real
cache miss fetching upstream successfully) is a UAT step — it needs a live
prefill of a game's missing chunks against the lancache; the request shape and
key alignment are already verified (spike A5).

— Senior Security Engineer persona, 2026-05-29
