# Changelog

All notable changes to this project will be documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/) with extended categories
for handoff clarity. Categories are ordered by impact severity.

<!--
  Category definitions:
  - Security: Vulnerability fixes, dependency patches for CVEs, auth changes
  - Data Model: Schema migrations, data format changes, rollback notes
  - Added: New features, new endpoints, new commands
  - Changed: Modifications to existing behavior
  - Fixed: Bug fixes (reference BUGS.md entry if applicable)
  - Removed: Removed features, deprecated endpoints
  - Infrastructure: CI/CD changes, dependency updates, configuration changes, tooling
  - Documentation: Significant doc updates (new ADRs, updated threat model, revised user guide)
-->

## [Unreleased]

### Changed — Re-arch ④ control-plane-to-LXC prep (2026-06-23) — 2026-06-23

Makes the control plane safe to run off-host (the final re-architecture step: brain → Proxmox LXC, agent stays on the UGREEN NAS). Code merged + Phase 0 live-verified on the NAS; the cutover itself is the operator runbook below.

- **Validator health gate is agent-aware (§3a):** when `agent_enabled`, `validator_self_test` sources `validator_healthy` from the data-plane agent's `GET /v1/health` (which now reports it, run against the agent's mounted cache) instead of stat-ing a local cache path the LXC won't have. The boot gate and the F13 sweep both pass `agent_client`; flag-off keeps the local-stat path byte-identical.
- **AgentClient connection reuse (§3b-1):** one persistent `httpx.AsyncClient` reused across calls, closed on lifespan shutdown — the cross-host control→agent link no longer rebuilds a client per request. The per-call 300s validate timeout is preserved.
- **Agent import decoupling (§3b-2 / ARCH-4):** moved `detect_non_loopback_bind` → `core/net.py` and the pure middleware constants → `api/_constants.py` so the data-plane agent process no longer transitively imports `api.main` or `db.pool` (proven by a subprocess import-graph guard).

### Documentation — LXC cutover runbook (2026-06-23) — 2026-06-23

- `docs/deploy/lxc-cutover-runbook.md`: the operator cutover (provision → parallel bring-up → atomic flip → live-verify) + rollback for moving the control plane to the Proxmox LXC.

### Removed — Code-review SEV-4 housekeeping (review 2026-06-23) — 2026-06-23

- Deleted the dead `adapters/{steam,epic}` + `status/` stub packages (imported nowhere; superseded by `platform/{steam,epic}`).
- Removed the dead CLI `auth steam` command — it POSTed to `/api/v1/platforms/steam/auth`, deleted in ③c (Steam auth is host-side SteamPrefill now). Epic auth + `auth status` remain.

### Changed — Code-review SEV-4 housekeeping (review 2026-06-23) — 2026-06-23

- **SEC-4:** Epic `AuthCodeBody` now sets `extra="forbid"`, matching the input-validation convention of the other request bodies (an unknown field is rejected with 400).
- **NAME-6/5:** collapsed the redundant `_steam_library_sync` pass-through wrapper into the real handler, giving symmetric `_steam_library_sync` / `_epic_library_sync` names.
- **NAME-9:** removed stale `steam_session_path` references from comments/docstrings (the setting was deleted in ③c).

### Documentation — ADR amendments (review 2026-06-23) — 2026-06-23

- **ARCH-1:** ADR-0013 (Steam subprocess isolation) marked **superseded** by re-arch ③c — the gevent/ValvePython worker it describes was deleted.
- **ARCH-2:** ADR-0001 amended — the Steam-subprocess thread + `adapters/` layer it described are no longer live (re-arch ②/③).

### Security — Code-review SEV-3 batch: SSRF + OOM hardening (review 2026-06-23) — 2026-06-23

- **SSRF (SEC-2/NEW-8):** the agent `/v1/pull` `host` field becomes the Host header lancache uses to pick an upstream, so an IP-literal / internal / port-or-path host could drive lancache to proxy-fetch arbitrary hosts (e.g. the `169.254.169.254` cloud-metadata endpoint). It's now validated as a plausible public FQDN, mirroring the Epic CDN-host guard (Steam + Epic CDN FQDNs still pass).
- **OOM (NEW-4):** the Epic manifest size cap was checked only after the full body was buffered. The download now streams with an incremental cap that aborts as soon as the running total exceeds the limit — a hostile/misbehaving CDN can no longer OOM the prefill.

### Fixed — Code-review SEV-3 batch: robustness/lifecycle (review 2026-06-23) — 2026-06-23

- **NEW-1:** the data-plane agent had no lifespan shutdown; on redeploy the dedicated cache-stat thread pool was leaked and in-flight prefill/pull tasks abandoned. The agent now cancels pending background tasks and shuts the executor down on stop.
- **COR-4:** `enumerate_library` paginated with an unbounded `while True`; a server returning a repeated cursor (or endless distinct cursors) could loop forever. Added repeated-cursor detection + a page-count backstop.
- **MEM-2/COR-7:** `AgentClient._post_then_poll` polled forever with no deadline and raised a raw `KeyError` on a 202 body missing `job_id`. Added a bounded poll deadline (`poll_timeout_sec`) and a clean `AgentError` on a malformed 202.
- **COR-2:** the SteamPrefill `.bin` parser surfaced any ChunkId bytes as a "SHA"; a non-hex / wrong-length / uppercase value would derive a wrong cache key and report a false miss. It now drops anything that isn't a 40-char lowercase-hex SHA1.
- **COR-5:** `EpicClient` token refresh wasn't serialized; concurrent callers double-spent the rotating (single-use) refresh token, invalidating the session. Refreshes are now serialized with a lock + a token-changed double-check so the loser reuses the fresh token.

### Removed — Code-review SEV-3 batch — 2026-06-23

- **CORE-2:** removed the dead prefill `force` flag — it was read from a job key the job row never carries (always `False` in production). The agent/driver `force` parameters remain (default `False`) for deliberate future use.

### Fixed — Code-review SEV-2 batch (multi-agent review 2026-06-23) — 2026-06-23

Four SEV-2 robustness/lifecycle defects found by a full-project review, fixed test-first:

- **Security/Logging (LOG-1):** `configure_logging()` was defined but never called in any production entrypoint, so the API and agent ran structlog's default `ConsoleRenderer` — the JSON one-line-per-event contract and the secret-redaction processor were silently absent. Now wired into `create_app()` (API) and `agent.__main__.main()` (agent) before the first log line.
- **Stability (CORE-1):** `Pool.write_transaction()` left the single writer connection mid-transaction if `COMMIT` failed (e.g. `SQLITE_BUSY`, which doesn't trip writer replacement), wedging every subsequent write with "cannot start a transaction within a transaction" until restart. The COMMIT is now wrapped with a best-effort `ROLLBACK`, mirroring `execute_write`.
- **Stability (COR-1):** the agent's `POST /v1/steam/validate` 500'd on a single corrupt/foreign `.bin` in the SteamPrefill cache (non-numeric depot field / unreadable file). Each manifest is now parsed in isolation — a bad one is logged and skipped; if every manifest fails, the endpoint returns a graceful `outcome="error"` instead of crashing the validate job.
- **Memory (MEM-1):** `AgentJobStore` grew unbounded on the long-lived agent (a `_Job` retained forever per prefill/pull/validate). It now bounds retention, evicting the oldest **terminal** (done/failed) jobs past a cap while never dropping a running job mid-flight.

### Removed — Legacy ValvePython Steam worker (re-arch ③c) — 2026-06-22

The gevent/`steam[client]` subprocess worker is fully deleted now that the data-plane agent owns Steam prefill, validation, and library enumeration via SteamPrefill (re-arch ① + ② + ③a/③b, all live).

- **Deleted** `platform/steam/{worker,client,protocol,session,enumerate}.py` (the worker, its IPC client, NDJSON protocol, session metadata, enumerate helpers), the `manifest_fetch` job handler + its `POST /api/v1/games/{id}/manifest/fetch` route + the `game manifest` CLI command, and the Steam `auth*` endpoints (`POST/GET /api/v1/platforms/steam/auth*`). Epic auth (separate router) is untouched.
- **Removed** the `venv-steam-worker` Docker build stage + `requirements-steam-worker.{in,txt}` (smaller image, no gevent), `Deps.steam_client`, and the worker settings (`steam_worker_*`, `steam_session_dir`, `steam_session_path`).
- The `steam_validate_via_agent` / `steam_enumerate_via_prefill` flags are collapsed — validate and library_sync now go through the agent unconditionally.

### Changed — validate + library_sync delegate to the agent unconditionally — 2026-06-22

- `validate_game` resolves the game's `app_id` and calls the agent's `/v1/steam/validate`; the legacy DB-manifest + `manifest_expand` + local cache-key path is gone.
- Steam `library_sync` always enumerates the prefilled library via the agent (`prefilled_apps` → Steam store type filter). The `manifests.raw` column is retained (Epic prefill writes it; the deferred F7-Epic validation will read it).

### Security — LAN-bind source-IP allowlist + fail-closed boot guard — 2026-06-18

- Source-IP allowlist (`SourceAllowlistMiddleware`) gates all API paths to loopback + `ORCH_ALLOWED_SOURCE_IPS` when bound off-loopback; defense-in-depth over the bearer token for LAN exposure.
- Fail-closed boot guard: a non-loopback bind with no `ORCH_ALLOWED_SOURCE_IPS` refuses to start.

### Added — `ORCH_ALLOWED_SOURCE_IPS` source-IP allowlist setting — 2026-06-18

- `ORCH_ALLOWED_SOURCE_IPS` setting (comma-separated IPs/CIDRs) + `Settings.allowed_source_networks`.

### Infrastructure — LAN-exposure deploy recipe documented — 2026-06-18

- Documented LAN-exposure deploy recipe (LAN-scoped docker publish + host nftables rule) in README.

### Added — F8 block list + scheduled prefill driver (version-diff) — 2026-06-17

The orchestrator becomes the automatic **Steam+Epic prefill driver** (completes F12's "diff enqueues prefills"): a 6h cycle prefills only the games that actually changed, with an operator block-list to exclude games.

- **Block-list REST resource** (`GET/POST/DELETE /api/v1/block-list`): paginated wrapped envelope + allow-list filters (`platform`/`source`), idempotent POST (`201` new / `200` existing, accepts an unknown `(platform, app_id)` for pre-blocking), idempotent DELETE (`{removed: 0|1}`). `block_list` is the single source of truth — no schema change (the table shipped in `0001_initial.sql`).
- **CLI** `game block <id> [--reason]` / `game unblock <id>` (resolve id→`(platform, app_id)` via the list, then POST/DELETE), and a `BLOCKED` column on `game list`. New `OrchClient.delete`.
- **`games.blocked`** (bool) on `GET /api/v1/games` via a correlated `EXISTS` subquery (no JOIN → no filter-clause ambiguity).
- **Scheduled prefill driver** (`enqueue_scheduled_prefill`, registered on the library-sync interval, `scheduled_prefill_enabled`): one diff insert enqueues `prefill` for owned games where `cached_version IS NULL OR cached_version <> current_version OR status='validation_failed'`, excluding block-listed ones (`ON CONFLICT DO NOTHING` + the migration-0006 in-flight index dedup).

### Changed — version tracking populates the dormant `current_version`/`cached_version` columns — 2026-06-17

- `library_sync` now writes `current_version` from enumeration: Steam = public-branch `buildid` (composite SHA-256 of sorted depot:gid as fallback), Epic = `buildVersion`. `COALESCE` preserves a known-good version when an enumeration carries none (never erases tracking).
- Prefill is the **sole** writer of `cached_version` (= `current_version`, on full success). Steam prefill now **re-fetches a fresh manifest when version-diverged** so a patched game caches *current* content (not the stale manifest); Epic already fetched fresh. Validate updates `status` but never `cached_version` (a standalone sweep can validate a stale manifest).

### Data Model — `current_version`/`cached_version` now populated; `block_list` now consumed — 2026-06-17

No migration. The two version columns (vestigial since `0001`) are now maintained, and the `block_list` table (also from `0001`) gains its first reader/writer.

### Security — block-list endpoints bearer-gated + bound-checked — 2026-06-17

All three block-list endpoints are behind the global bearer middleware; inputs are bounded by pydantic `Literal`/`Field` AND the table CHECK constraints; all SQL is parameterized (allow-list field names only). An adversarial-review pass found and we fixed two SEV-2 correctness defects test-first (Epic infinite re-prefill; steam stale-manifest false version-stamp) and one SEV-4 (CLI `unblock` now URL-encodes `app_id`). Full suite **1253 pass**; mypy(strict)/ruff/semgrep/gitleaks clean. See `docs/security-audits/f8-block-list-security-audit.md`.

### Infrastructure — prefill logs WHY chunks failed (#169, step 1) — 2026-06-16

A failed prefill recorded only the failure **count** (`prefill: 2430/2430 chunks failed`), so diagnosing it meant code-spelunking. The downloader already captured per-chunk `(uri, reason)` tuples; they just weren't surfaced.

- `prefill.completed` (steam) and a new `prefill.epic.chunks_failed` (epic) now carry a `failure_reasons` histogram (top reasons → counts, e.g. `{'http 403': 2418, 'ConnectError': 12}`), and the game's `last_error` includes the dominant reasons — so `403`-needs-token vs `404`-not-found vs `ConnectError`-lancache-down is visible from the CLI/API and logs.
- **Tests:** `_summarize_failures` (count + top-N), and `last_error` includes the reason. Full suite **1212 pass**; mypy(strict)/ruff/semgrep clean.
- Step 1 of #169 (the F5 prefill download failing on newly-versioned Steam chunks): this makes the cause diagnosable; the actual download fix follows once the live HTTP status is read.

### Fixed — steam worker restarts on a wedged op (#153 / F-INT-6) — 2026-06-16

When a steam op timed out (`IPCTimeoutError`) or was cancelled (job-runtime timeout → `CancelledError`), the **serial** worker subprocess was left wedged on the abandoned op, and the next steam job queued behind it (potentially timing out too).

- `SteamWorkerClient` now flags `_needs_restart` on such a timeout/cancel and, before the **next** op, SIGKILLs the wedged worker and respawns a fresh one (`_restart_after_wedge`). Per the operator decision, this **loses the logged-in Steam session** (the worker doesn't auto-relogin), so the next credentialed op returns `NotAuthenticated` and the orchestrator's auth-flip prompts a re-auth — an accepted cost vs. a worker stuck behind an unkillable op. The deliberate SIGKILL is **not** counted against the restart-storm guard (`_intentional_restart` suppresses the EOF death callback).
- **Concurrency-correct** (adversarial review caught these): the client is shared between the jobs loop and the API auth endpoints, so the restart-check + send are serialized under a new `_ipc_lock` (released before the long `_await_response`, so responses still correlate concurrently); `_needs_restart` is cleared only after a **confirmed respawn** (so a cancelled/failed restart retries rather than reusing a dead worker); `proc.wait()` is bounded (5s) so an unkillable process can't hang the restart; and a cancel mid-send re-flags + drops the orphan pending future.
- **Tests:** `TestWorkerRestartOnWedge` (6) — flag-on-timeout/cancel, restart-before-next-op, `shutdown` exemption, storm-guard suppression, and **restart-exactly-once under concurrency**. Two adversarial review passes (the second confirmed all findings resolved). Full suite **1209 pass**; mypy(strict)/ruff/semgrep clean.

### Fixed — mid-fetch auth loss via EResult + manifest double-compression (#122, #123.1) — 2026-06-15

- **#122 (redone correctly):** a Steam session lost *mid* manifest fetch was masked as a generic `SteamAPIError`, so the orchestrator's auth-flip (`platforms.auth_status='expired'`) never fired and the operator wasn't told to re-auth. The fix maps a **genuine auth-loss `EResult`** (steam-next's `SteamError.eresult` ∈ `{NotLoggedOn, LoggedInElsewhere, Expired, Revoked, AccessDenied}`) to `kind=NotAuthenticated`. Crucially, transient results — notably `EResult.Timeout` from a slow/dropped CM — stay a **retryable** `SteamAPIError`, so a network blip never forces a needless 2FA re-auth. This replaces the first attempt (a `connected`/`logged_on` socket-state proxy), which an adversarial review flagged as a SEV-2 (socket-up ≠ credentials-valid). An unambiguous session-wide loss surfacing on a per-depot call now fails the whole fetch (vs. a false-partial success); `AccessDenied` on a per-depot call stays a skip (the "depot not owned" case is indistinguishable there).
- **#123.1:** the worker stored `zstd(serialize())` but `serialize()` defaults to ZIP-compressing, so blobs were `zstd(ZIP(protobuf))` — double-compressed. Now stores `zstd(serialize(compress=False))` = `zstd(protobuf)` (smaller). Verified against steam-next 1.4.4: `DepotManifest.deserialize` auto-detects (tries `ZipFile`, falls back to raw), so the F7 `manifest.expand` round-trip and already-stored `zstd(ZIP(pb))` blobs both still parse.
- **Tests:** five new `test_manifest_fetch_*` cases including the **anti-regression** `..._transient_timeout_eresult_is_not_auth_loss` (Timeout must stay retryable) and `..._per_depot_access_denied_is_skipped`. Adversarial review: **no defects** (verified `EResult.Timeout` ∉ the auth sets against the live library). Full suite **1203 pass**; mypy(strict)/ruff/semgrep clean. Live-validated against the real Steam CDN (worker live-round).

### Documentation — #123.3 trigger `job_id` race verified resolved + guarded — 2026-06-15

The #123.3 concern (a trigger endpoint's `ORDER BY id DESC LIMIT 1` re-read returning the *wrong* `job_id` under concurrency) is **obsolete**: the prefill/validate/manifest triggers already re-select filtered by `(kind, game_id, in-flight state)`, backed by the migration-0006/0007 partial UNIQUE indexes + `INSERT ... ON CONFLICT DO NOTHING`, so the returned id is deterministic per game. The issue's proposed `lastrowid` fix would in fact be *unreliable* here (a no-op `ON CONFLICT` INSERT leaves `lastrowid` stale). No code change needed.

- Added `test_concurrent_triggers_across_games_return_correct_per_game_id` — fires concurrent validate POSTs across distinct games and asserts each response's `job_id` belongs to *its* game, locking the invariant in so a future regression to a global re-select is caught.

### Infrastructure — dedicated bounded executor for cache stat I/O (#123.4) — 2026-06-15

`validate_chunks` offloaded its `stat()` batches to the **shared default** thread pool (`run_in_executor(None, ...)`). asyncio also uses that pool for stdlib offloads such as `getaddrinfo` (DNS), so a hung NFS cache mount filling it with blocked `stat()` threads could starve the pool and stall the orchestrator's own HTTP probes (lancache heartbeat, Epic API) — a cross-subsystem failure from one bad mount.

- Cache stat I/O now runs on a **dedicated, bounded `ThreadPoolExecutor`** (`thread_name_prefix="cache-stat"`, 2 workers — the batch loop is sequential and the jobs worker is serial, so the bound is for isolation, not parallelism). A hung mount now stalls at most validation, never DNS/HTTP.
- The pool is created lazily (on the event-loop thread, so no creation race) and torn down in the app-lifespan shutdown (`shutdown_cache_stat_executor()`, idempotent; `cancel_futures=True` so a hung-mount backlog can't block shutdown). Ordering: the jobs worker is already stopped before the executor shuts down, so no validation is in flight.
- **Tests:** `test_validate_chunks_uses_dedicated_cache_stat_pool` (stats run on a `cache-stat` thread, not the default pool), `test_shutdown_cache_stat_executor_is_idempotent_and_recreates`. Full suite **1192 pass**; mypy(strict)/ruff/semgrep clean.

### Fixed — manifest `chunk_count` is now unique chunks, not summed refs (#121, #123.2) — 2026-06-15

`_handle_manifest_fetch` stored `chunk_count` as `sum(len(mapping.chunks))` across all file mappings, which double-counts content-deduped chunks (Steam shares one chunk across many files). F7's `manifest.expand` + `validate` dedup by SHA, so the operator saw e.g. "1820 chunks" (BL12) vs the validator's "1100" for the same depot — confusing, and the "all cached" arithmetic looked inconsistent.

- `chunk_count` now uses the protobuf's true value, `ContentManifestMetadata.unique_chunks` — the same unique count F7 validates against. If that field is *absent* (a steam-next rename / older protobuf) it falls back to the summed refs **and warns to stderr** (drained by the orchestrator), so a silent revert to double-counting is visible rather than masked. A legitimately-empty depot (`unique_chunks == 0`) is preserved as 0.
- **#123.2:** dropped the dead `name` field from the manifest IPC payload — the orchestrator handler never consumed it.
- **Tests:** `test_manifest_fetch_chunk_count_is_unique_not_summed`, `test_manifest_fetch_chunk_count_falls_back_when_field_absent`. Full suite **1192 pass**; mypy(strict)/ruff/semgrep clean.
- **Deferred (adversarial review):** #122 (mid-fetch `NotAuthenticated` masking) — the quick `connected`/`logged_on` proxy conflates a transient CM socket drop with real auth loss, which would force a needless 2FA re-auth (SEV-2); it will be redone with EResult-based detection. #123.1 (double-compression) — changes the stored BLOB format and needs the F7 expand round-trip validated live first.

### Fixed — `GET /api/v1/manifests` now exposes `depot_id` (#127) — 2026-06-15

UAT-9 live finding. Migration `0003` added `manifests.depot_id` (populated correctly by the BL12 manifest fetch — verified live), but the BL9 read endpoint's `SELECT` and `ManifestResponse` model predated the column, so every row came back `depot_id: null`. The DB was correct; this was purely a read-side exposure gap (F7's validator reads the DB directly, so it was unaffected).

- `depot_id` is now in the manifests `SELECT`, the `ManifestResponse` model (`int | None` — nullable for rows written before the column existed), and the row mapping.
- **Tests:** `TestManifestDepotIdExposure` (a stored `depot_id` is returned; a NULL one stays `null`); the per-manifest field-set test now includes `depot_id`. Full suite **1191 pass**; mypy(strict)/ruff/semgrep clean.

### Security — bump starlette 1.1.0 → 1.3.1 (2 CVEs) — 2026-06-15

A newly-disclosed advisory flagged `starlette==1.1.0` with two vulnerabilities (`GHSA-82w8-qh3p-5jfq`, `GHSA-jp82-jpqv-5vv3`), failing the CI `Dependencies` (pip-audit) gate on every branch including `main`.

- Pinned `starlette==1.3.1` (the fix version) explicitly in `requirements.in` and recompiled the hashed `requirements.txt`, mirroring the existing `python-dotenv` CVE-override precedent. `fastapi==0.136.3` requires only `starlette>=0.46.0` (no upper bound), so this is compatible **without** a FastAPI bump; only `starlette` moved.
- Verified: `pip-audit --requirement requirements.txt --disable-pip --strict` → "No known vulnerabilities found"; full suite **1192 pass** against starlette 1.3.1.

### Fixed — DB pool writer now self-heals after a replacement storm (#152 / F-INT-2) — 2026-06-15

UAT-11 integration finding. Readers already self-heal after a replacement storm (`_lost_reader_slots` + heal-on-acquire), but the writer did not: once the writer storm guard tripped (or a replacement open failed), `self._writer` was left pointing at the dead connection with `_writer_healthy=False` and `_checkout_writer` kept yielding it — **every write failed until a process restart** (health_check live-probes the writer → 503 → HEALTHCHECK container restart). SEV-3, recovery-by-restart, asymmetric with readers.

- `_checkout_writer` now heals-on-checkout when the writer is known-dead: it opens a fresh writer connection (under `_writer_lock`, so it can't race a concurrent replacement swap), restores `_writer_healthy=True`, and best-effort-closes the old one — so writes recover **without a restart** once the fault clears. If the heal-open itself fails (persistent fault), it raises `PoolError` (loud → 503) rather than yielding a dead/closed connection.
- **Tests:** `tests/db/test_pool_writer_self_heal.py` — recovery after the fault clears, and the loud-`PoolError` path when the heal-open fails. Full suite **1192 pass**; mypy(strict)/ruff/semgrep clean.

### Fixed — Steam worker crash on slow-CDN `gevent.Timeout` — 2026-06-14

Root-caused via the new stderr drain (below) during the UAT-11 live leg: a manifest fetch for a Steam app with a slow CDN depot crashed the worker process (`steam_worker.died reason=stdout_closed`), failing that job **and every subsequent job until restart**. The captured stderr showed `gevent.timeout.Timeout: 15 seconds`.

- **Root cause:** `gevent.Timeout` subclasses `BaseException` (by gevent's design, so a stack-unwinding timeout can't be swallowed by a bare `except Exception`). steam-next's CDN/CM calls raise it when a depot is slow, so it escaped every `except Exception` in the worker handlers **and** the dispatch loop, terminating the process. This is intermittent and CDN-timing-dependent — which is why the same app (211) succeeded in 4s on one attempt and a different app (340) crashed at 15s on another, and why it was never the `#15` manifest-cleanup change (a `BaseException` bypasses try/except regardless of wrapping).
- **Fix (defense in depth):** `worker.py` now imports `gevent.Timeout` and (1) the `manifest.fetch` handler catches it → clean **retryable** `SteamCDNTimeout` error (it fails the job rather than recording a partial manifest set as success — the `#109` false-empty lesson — and cleans its temp BLOBs), and (2) the worker's `main()` dispatch loop wraps every handler so **no** op's `gevent.Timeout` (or any unexpected exception) can take the worker down.
- **Tests:** `test_manifest_fetch_gevent_timeout_does_not_crash_worker`, `test_dispatch_loop_survives_handler_gevent_timeout` (the `gevent` stub's `Timeout` subclasses `BaseException` to faithfully reproduce the escape). Full suite **1190 pass**; mypy(strict)/ruff/semgrep clean.

### Fixed — Steam worker stderr is now drained + captured — 2026-06-14

Caught during the UAT-11 live leg: the Steam worker subprocess crashed ~25s into an `app_id=211` manifest fetch with `steam_worker.died reason=stdout_closed` and **zero diagnostics** — the worker's `stderr` was a `PIPE` that nothing ever consumed, so its traceback (the only window into the opaque gevent/steam-next subprocess) was discarded.

- The orchestrator now runs a dedicated task that drains the worker's `stderr` line-by-line for the worker's lifetime, logging each as `steam_worker.stderr` and retaining the last 50 lines in a ring buffer. The `steam_worker.died` (and restart-storm-guard) breadcrumb now carries the last 10 stderr lines, so a crash is self-explaining (Python traceback, or a native `Segmentation fault` line).
- Also closes a latent stall: an undrained `stderr` PIPE fills its ~64 KiB OS buffer under a chatty worker and blocks the next write — freezing the whole gevent hub mid-fetch. Draining removes that failure mode.
- **Tests:** `TestWorkerStderrDrain` (ring capture, death-log breadcrumb, drain task wired in `start()`). Full suite **1188 pass**; mypy(strict)/ruff/semgrep clean.

### Fixed — Docker image entrypoint + console-script shebangs — 2026-06-13

Caught during the UAT-11 live deploy: the runtime container failed to start (`sh: exec: uvicorn: not found`).

- The venv is built at `/build/.venv` and `COPY`'d to `/app/.venv`, so every pip console script hardcodes a `#!/build/.venv/bin/python` shebang that doesn't exist in the runtime image — breaking both the `uvicorn` entrypoint and the **bundled `orchestrator-cli` console script** (it would `ENOENT` inside the container). The Dockerfile now rewrites those shebangs to `/app/.venv/bin/python` after the copy.
- The entrypoint now runs `python -m uvicorn` (shebang-independent) rather than the `uvicorn` console script, while keeping the `ORCH_API_HOST` loopback default from F-INT-3.

### Fixed — UAT-11 remediation — 2026-06-13

Remediation of the UAT-11 findings (automated PASS; exploratory + integration legs). All fixed test-first.

- **Fixed (cli, SEV-2):** a missing `ORCH_TOKEN` produced a raw ~15-frame traceback from the in-process `config`/`db` commands — `handles_local_errors` caught `ValidationError` but not the scrubbed plain `ValueError` that `Settings.__init__` re-raises for the missing token. Now catches `ValueError` (which subsumes `ValidationError`). (S11-E-01)
- **Fixed (jobs, F-INT-1 — regression from the per-job timeout):** the max-runtime timeout cancels the handler via `CancelledError` (a `BaseException`), which bypasses the prefill/validate "never leave the game `downloading`" guard — leaving the game stuck. Now the worker (which is *not* cancelled) resets the game on timeout, and a new boot-time `reap_orphaned_game_status` reaper recovers any game left `downloading` by a crash. (F-INT-1)
- **Data Model:** migration `0007_jobs_manifest_fetch_unique` adds the in-flight UNIQUE index `manifest_fetch` was missing (the last job kind without one); `manifest_trigger` now `INSERT ... ON CONFLICT DO NOTHING`. (F-INT-5)
- **Security/Deploy (F-INT-3):** the Dockerfile no longer hardcodes `--host 0.0.0.0` — it binds `ORCH_API_HOST`, defaulting to loopback (`127.0.0.1`), so the trigger endpoints aren't exposed to the LAN by default and the non-loopback boot warning only fires when intentionally opted in.
- **Fixed (cli, UX):** wrong Steam/Epic credentials now surface the server's 401 detail instead of the misleading "check ORCH_TOKEN" (S11-E-03); `--state`/`--kind`/`--status` are `click.Choice`-validated so a typo is rejected up front instead of silently returning an empty table (S11-E-04); `game <id>` rejects non-positive ids with an actionable message (S11-E-05); `config show` no longer over-redacts URL fields like `epic_token_url` and auto-sizes the key column (S11-E-06/10); the noisy `/run/secrets does not exist` warning is suppressed on the operator path (S11-E-07); `db vacuum` errors name the DB path (S11-E-08); `python -m orchestrator.cli.main` works via a `__main__` guard (S11-E-09); `--limit` help documents the 500 cap (S11-E-11).
- **Documentation:** new **Operator CLI** section in the README (invocation, `ORCH_TOKEN`, commands, exit-code contract) and the env-var table now lists the validation-sweep, `ORCH_JOB_MAX_RUNTIME_SEC`, and `ORCH_STEAM_LICENSE_WAIT_SEC` settings. (F-INT-4 + CLI-undocumented gap)
- **Deferred to follow-up issues:** F-INT-2 (writer-connection self-heal — surfaces via health→503) and F-INT-6 (free the serial steam worker on a timeout-cancel).
- **Tests:** regression tests across `tests/cli/`, `tests/jobs/`, `tests/db/`, `tests/test_dockerfile.py`. Full suite **1183 pass**; mypy(strict)/ruff/gitleaks/semgrep clean.

### Added — jobs worker operability (eval quick-wins) — 2026-06-13

Two operability improvements to the jobs worker, from the post-audit codebase eval.

- **Logging:** each job now runs inside its own `correlation_id` (plus `job_id`/`job_kind`) bound into contextvars — the job-side analogue of the HTTP `request_context()`. Every log line a job emits (worker → handler → validator → pool) now carries the same `correlation_id`, so a job's full lifecycle is greppable by one ID. Previously only the HTTP path had correlation IDs; job logs could only be tied together by manually-passed `job_id`.
- **Self-recovery:** a configurable per-job wall-clock budget (`job_max_runtime_sec`, default 6h, `0` disables) wraps each handler in `asyncio.wait_for`. A wedged handler (e.g. a hung steam-IPC call) is cancelled and the job marked failed with a `jobs.handler.timed_out` log — so it can no longer hold the single worker loop forever, and the system self-heals **without a process restart**. Chosen over a standalone periodic reaper because, in the single-worker topology, timing out the handler also frees the worker (a reaper can't). A false timeout on a genuinely-long prefill is non-catastrophic: the partial cache persists and a retry resumes from it.
- **Tests:** `test_job_logs_share_a_correlation_id`, `test_hung_handler_times_out_and_marks_failed`. Full suite **1165 pass**; mypy(strict)/ruff/semgrep clean.

### Fixed — full-codebase audit remediation — 2026-06-09

Remediation of the confirmed (2-skeptic verified) findings from the multi-agent full-codebase audit. The `db/pool.py` concurrency findings shipped separately; the rest are collected here, each fixed test-first.

- **Fixed (db migrate, SEV-3):** apply-time SQL errors are now wrapped in `MigrationError` with a **scrubbed** message (`type(e).__name__` only) instead of escaping as a raw `sqlite3` exception. The previous behaviour broke the documented contract and the API-lifespan `except MigrationError`, and reflected SQLite's raw error text (including literals) into operator output.
- **Fixed (db migrate, SEV-3):** macOS filesystem-type detection now parses `mount` (the real fstype — `nfs`/`smbfs`/`apfs`) instead of `stat -f %T`, which returns the inode file-type *sigil* and never a network-FS name — silently defeating the WAL-on-network-FS corruption guard on darwin.
- **Security (steam, SEV-3):** the Steam worker now sweeps expired 2FA challenges on every `auth.begin`, so an abandoned login flow's cleartext password no longer lingers in worker-process memory past its 5-minute TTL.
- **Security (steam, SEV-3):** the steam-next credential directory (holds the long-lived refresh token) is created `0700` + chmod'd, not left at the umask default — it was world-traversable, weaker than the non-secret session metadata.
- **Fixed (steam, SEV-3):** `library.enumerate` now waits a configurable, much longer interval for the Steam license list (default 60s vs. the old hardcoded 10s) and **signals a retryable `LicenseListTimeout`** when it never populates — instead of returning a false empty library that the orchestrator recorded as a green zero-game sync.
- **Fixed (steam, SEV-4):** `manifest.fetch` now tracks every BLOB temp file it writes and deletes them on a mid-loop failure — previously, a depot raising after earlier depots wrote temp files leaked those files permanently (the orchestrator never learns their paths on the error path), accumulating on the container FS.
- **Tests:** first unit coverage for the gevent worker via a `sys.modules` steam-next stub harness (`tests/platform/steam/test_worker_audit.py`).
- **Fixed (epic, SEV-3):** `POST /platforms/epic/auth` now catches an `OSError` from persisting the refresh token (read-only/full FS, symlink at the path) and returns a clean `503` instead of leaking an unhandled `500` — and never reflects the tokens. Previously the consumed one-time OAuth code was burned with the operator left on a 500.
- **Fixed (epic, SEV-3):** the documented *401-forces-refresh* contract is now implemented — `EpicClient` forces a token refresh and retries once when a library/manifest call returns `401` (token revoked early, or a missing/unparseable expiry that defeated the proactive refresh). Previously such a token deadlocked the process-singleton client until restart, failing every Epic job. `EpicLibraryError`/`EpicManifestError` now carry the upstream `status_code`.
- **Data Model:** migration `0006_jobs_prefill_validate_unique` adds partial UNIQUE indexes enforcing **at most one in-flight `prefill` and one in-flight `validate` per game** (mirrors the `library_sync`/`sweep` guards from 0004/0005). Pre-existing duplicates are cancelled before the index is created.
- **Fixed (jobs, SEV-3):** `prefill_trigger` (and `validate_trigger`) now `INSERT ... ON CONFLICT DO NOTHING` against that index, so concurrent triggers (operator double-click, CLI racing the API) can no longer queue duplicate prefills/validations — previously the non-atomic SELECT-then-INSERT raced into duplicate full downloads.
- **Fixed (jobs, SEV-4):** the steam prefill handler's auto-enqueue of a `validate` job is now `ON CONFLICT DO NOTHING`, so it no longer piles up redundant validate rows that burn the serial steam slot.
- **Fixed (prefill, SEV-3):** both the Steam and Epic chunk downloaders now catch `httpx.RequestError` (covers `DecodingError` from a corrupt/mislabeled `Content-Encoding`), so one bad chunk is recorded as a single failure instead of escaping `asyncio.gather` and aborting the entire game's prefill (cancelling every sibling chunk).
- **Fixed (jobs, SEV-4):** the jobs worker now retries a transient pool error on the terminal status write (`mark_succeeded`/`mark_failed`) before giving up, shrinking the window where a successful job is left stuck `running` for the next-boot reaper to mislabel `failed`.
- **Fixed (validator, SEV-3):** `validator_self_test` now fails when the cache directory is **empty AND not a mountpoint** — the signature of an unmounted Docker bind-mount/volume (Docker silently auto-creates an empty dir at the target). Previously it reported healthy and the validator then flagged every cached game as missing. A correctly-mounted-but-fresh cache is still a mountpoint, so it passes.
- **Fixed (cli, SEV-3):** the in-process `config show` / `db migrate` / `db vacuum` commands now surface a clean exit 1 (not a raw pydantic traceback) when a malformed `ORCH_*` env var makes `get_settings()` raise `ValidationError`. The shared decorator (renamed `handles_local_errors`) now also catches `ValidationError`, and `config show` is wrapped by it.
- **Fixed (settings, SEV-4):** the `config.secret_shadowed_by_env` diagnostic now matches `os.environ` case-insensitively, so a lowercase `orch_token` env var (which `case_sensitive=False` accepts and lets shadow the secrets file) also triggers the warning — previously only the uppercase `ORCH_TOKEN` did.

### Fixed — DB pool concurrency (full-codebase audit) — 2026-06-09

A multi-agent codebase audit surfaced three `db/pool.py` concurrency defects (each 2-skeptic verified); all fixed test-first.

- **Security/Stability (SEV-2):** the reader heal path was an unsynchronized check-then-act on `_lost_reader_slots` — under a reader deficit plus concurrent reads, two acquirers both passed the `> 0` guard before either decremented, both minted a reader, over-healed past `readers_count`, drove the deficit negative (suppressing all future heals), and the surplus reader's release `put()` into the bounded queue **blocked forever** (connection leak + hung request). This reopened the previously-fixed reader-exhaustion deadlock on the heal side. Fixed by serializing the heal check-then-act under a new `_heal_lock`, so the deficit strictly bounds the number of heals and can never go negative.
- **Stability:** the reader release and replacement paths now use `put_nowait` instead of a blocking `put()`, so a release can never hang even if the queue is unexpectedly full (defense in depth for the overflow above). A surplus reader is *dropped from circulation* (left in `_reader_pool` for shutdown to close) — **not** background-closed, which would race `_teardown_connections` into an aiosqlite double-close deadlock. `_teardown_connections` now also closes queued readers (deduped by `id()`), fixing a latent connection/thread leak.
- **Stability (SEV-4):** two concurrent writer replacements for the same broken connection both assigned `self._writer`, leaking the first. Fixed with a compare-and-swap under `_writer_lock` (`if self._writer is old_conn`); the loser closes the connection it opened instead of leaking it.
- **Tests:** `tests/db/test_pool_concurrency_audit.py` (3 regression tests — concurrent over-heal, release-never-blocks-on-full-queue, concurrent-writer-surplus-closed). Full suite **1139 pass**; mypy(strict)/ruff/gitleaks/semgrep clean.

### Added — F11 `orchestrator-cli` — 2026-06-08

Implements F11 (Manifesto §50) — a Click-based operator CLI bundled in the container that drives the local REST API with the bearer token. The console entry `orchestrator-cli` was already declared in `pyproject.toml`; this fills it in.

- **Added:** `src/orchestrator/cli/` package — `OrchClient` (sync `httpx.Client` wrapper with a MockTransport test seam + exit-code-bearing exceptions), colorblind-safe `output` helpers (icon + text label, never color alone — Intake §9), a root `cli` group, and seven command modules. Commands: `auth steam|epic|status` (interactive Steam 2FA + Epic code, both prompted hidden, never echoed), `library sync`, `status`, `game list|show|prefill|validate|manifest`, `jobs`, `db migrate|vacuum`, `config show`. All HTTP except `db`/`config`, which run **in-process** (schema/maintenance ops are never exposed over HTTP). `game show` filters the list endpoint (no `GET /games/{id}`).
- **Changed:** `pyproject.toml` `[project.scripts]` entry repointed to `orchestrator.cli.main:main` (the exit-code-mapping wrapper).
- **Errors:** API unreachable → exit 2, auth failure (401) → exit 3, other → 1 (Manifesto F11). No `--json` (deferred, OQ6); `game block|unblock` deferred to the F8 block-list API. A backstop in `handles_api_errors` turns a malformed 2xx body (a missing/renamed field) into a clean exit 1 instead of a raw traceback; every `httpx.TransportError` subclass (incl. a mid-deploy server disconnect) **and** an `httpx.InvalidURL` from a fat-fingered `--url` map to exit 2; the in-process `db migrate`/`db vacuum` commands get a `handles_db_errors` wrapper so a `MigrationError`/sqlite failure is a clean exit 1, not a stacktrace.
- **Security:** the API's `RequestValidationError` handler (`api/main.py`) now strips the rejected `input` (and `ctx`/`url`) from the 400 detail, keeping only `type`/`loc`/`msg`. FastAPI's default payload echoed the raw request body — so a malformed `POST /platforms/steam/auth` reflected the **submitted password** straight back in the response. This also closes the UAT-10 deferred "validation-error input echo" item. `config show` redacts secret-bearing fields by **name** (`token`/`secret`/`password`), so the plain-`str` `epic_client_secret` is masked, not only the `SecretStr` `orchestrator_token`.
- **Tests:** 61 CLI tests via Click `CliRunner` + `httpx.MockTransport` — exit-code mapping, the Steam 2FA 200/202→200 paths (asserting creds/codes are never echoed), `config show` redaction, `db migrate`/`vacuum` happy-path **and** error-path (MigrationError / non-SQLite file → clean exit 1), the `limit` pagination param (not `per_page`), `game show` found/not-found, every `httpx.TransportError` subclass + a malformed `--url` → exit 2, `/health` 503-degraded rendering, ragged-table tolerance, and the malformed-2xx backstop; plus a server-side test that a body validation error never reflects the submitted password. Full suite **1136 pass**; mypy(strict)/ruff/gitleaks/semgrep clean. Two 4-lens adversarial-review passes (secret-leak / exit-code / HTTP-correctness / robustness) were run over the batch and each finding skeptic-verified; the confirmed defects (credential reflection, `epic_client_secret` leak, transport/URL error escapes, `db` error escapes) were fixed in-batch test-first — see `docs/security-audits/f11-cli-security-audit.md`. New deps: none (click/httpx/aiosqlite already direct).

### Added — F13 Scheduled Validation Sweep — 2026-06-07

Implements F13 (Manifesto OQ7) — a scheduled job that re-runs F7 disk-stat validation across the cached Steam library to catch **LRU eviction drift** (games that were `up_to_date` flip to `validation_failed` when evicted) and **recovery** (`validation_failed` → `up_to_date` when re-cached). Keeps `games.status` honest over time with no operator action. Follows the F12 pattern: the scheduler enqueues, the jobs worker executes.

- **Data Model:** migration `0005_jobs_sweep_unique.sql` — partial UNIQUE index `idx_jobs_sweep_inflight` (≤1 queued/running `sweep` job), mirroring the library_sync guard; `CHECKSUMS` updated.
- **Added:** a second cron job on `SchedulerManager` (`CronTrigger.from_crontab`, default `"0 3 * * 0"` = Sundays 03:00 UTC) firing a thin, never-raises `enqueue_validation_sweep` callback (`ON CONFLICT DO NOTHING`); a new `sweep` job handler that pre-flight-skips on validator-unhealthy / no-steam-client (job succeeds), enumerates steam games with status `up_to_date`+`validation_failed`, validates them 10-at-a-time (`Semaphore(sweep_batch_size)`) with per-game error isolation, and emits a `sweep.completed` summary (total / by-outcome / evicted / recovered / errors).
- **Changed:** extracted `validate_one_game()` from the validate handler so the F7 validate job and the F13 sweep share identical record/status logic (incl. the UAT-10 #3 transient-`downloading` rule); the validate handler is now a thin wrapper (behaviour unchanged).
- **Settings:** `validation_sweep_enabled` (default true), `validation_sweep_cron` (default `"0 3 * * 0"`, fail-fast validated via `CronTrigger.from_crontab`), `sweep_batch_size` (default 10, ge 1).
- **Tests:** settings (cron fail-fast), migration dedup invariant, enqueue callback (never-raises + dedup), scheduler registration (enabled/disabled), `validate_one_game` parity, sweep handler (skip paths, enumeration filter, per-game isolation, registration), and boot wiring (disable via `ORCH_VALIDATION_SWEEP_ENABLED`). Full suite **1074 pass**; mypy(strict)/ruff/gitleaks/semgrep clean. A 4-lens adversarial review caught and fixed in-batch (test-first) two SEV-3 defects: the `evicted` metric miscounting `error` outcomes (now gated on a genuine `partial`/`missing` regression, with `validation_error` surfaced), and the sweep's 10-wide concurrency outrunning the strictly-serial steam worker (now a single-flight `Lock` on `SteamWorkerClient.manifest_expand` so trailing requests don't spuriously time out). Security audit: 0 open findings (`docs/security-audits/f13-scheduled-sweep-security-audit.md`).
- **Deferred (follow-ups):** Epic disk-stat sweep (F7-Epic), manifest-version pruning (keep latest 3), incremental/changed-manifest-only validation, the `SWEEP_WARN_HOURS` status-page banner.

### Fixed — UAT-10 remediation (F5/F6 automated sweep) — 2026-06-04

Remediates the 11 confirmed findings (+ 1 observation) from the UAT-10 automated
adversarial sweep over F5 (Steam prefill) and F6 (Epic prefill). Each fix landed
test-first; full suite **1051 pass**, mypy(strict)/ruff/gitleaks/semgrep clean.
Security audit: `docs/security-audits/uat10-remediation-security-audit.md`.

- **Security — Epic manifest decompression bomb (SEV-2):** `platform/epic/manifest.py` inflated the zlib body with unbounded `zlib.decompress`; a tiny compressed manifest (under the 128 MiB *compressed* cap) could expand to GBs and OOM the process. It now uses a bounded `zlib.decompressobj().decompress(body, max_decompressed)` (256 MiB default) and raises `EpicManifestError` before allocating beyond the cap.
- **Security — Epic manifest fetch SSRF (SEV-3):** `fetch_manifest` GET-ed the response-supplied CDN `uri` *before* validating it; the FQDN + `..` guards ran only afterward. The host (FQDN) and path-traversal checks now run **before** the GET, so a hostile/MITM'd Epic response can no longer drive the manifest fetch at an internal host/IP.
- **Changed — Epic prefill is now triggerable (SEV-3):** `POST /api/v1/games/{id}/prefill` was hardcoded to `steam` (400 for non-steam), leaving the Epic prefill handler reachable only by a manual DB insert. The trigger now accepts `steam`/`epic` and enqueues a job carrying the game's own platform; an unsupported platform still returns 400.
- **Fixed — Steam prefill stuck `downloading` (SEV-3):** an IPC/worker/auth failure during manifest expand/fetch left the game in `downloading` forever (only the *jobs* row was marked failed). `_steam_prefill` now wraps its work and marks the game `failed` (`WHERE status='downloading'`), mirroring the Epic path.
- **Fixed — post-prefill validate error leaves game stuck (SEV-3):** a successful prefill followed by a `validate` returning `outcome='error'` (e.g. cache unmounted) never resolved the transient `downloading` status. The validate handler now resolves only that transient state to `failed` (scoped `WHERE status='downloading'`) without clobbering an already-classified status.
- **Fixed — Epic OAuth success-path error contract (SEV-4):** a malformed/non-JSON HTTP 200 from the token endpoint raised a raw `KeyError`/`JSONDecodeError` (→ a 500 instead of the documented 401). It now raises `EpicAuthError`, logging only `what`+`status` (never the body/token).
- **Changed — Steam auth auto-enqueue:** switched `_queue_library_sync_job_best_effort` to atomic `INSERT ... ON CONFLICT DO NOTHING` (was a SELECT-then-INSERT straddling an `await`), consistent with the other four call sites.
- **Tests:** added regression tests for every fix above, plus the previously-missing coverage the sweep flagged — Epic downloader retry (503→200 recovery, transport-error retry-then-fail), `verify_cached` MISS-ratio + non-gating low-ratio warning, the CDN `..` traversal guard, and the `O_NOFOLLOW` symlink/TOCTOU guard. Removed a redundant `pytest.mark.asyncio` that warned on two sync tests.

### Added — F6 Epic CDN Prefill (full Epic stack) — 2026-06-03

Implements F6 from PROJECT_BIBLE §1.2 — brings Epic Games to parity with the Steam pipeline: OAuth → library enumeration → manifest fetch + parse → chunk prefill through the lancache → cache-HIT verification. All **pure-Python / async-httpx in the orchestrator process** — no `legendary` runtime dependency, no gevent, no worker subprocess (unlike Steam's ADR-0013 isolation). De-risked end-to-end by `spikes/spike_b_epic_prefill.py` (PASS). **No DB migration** — the schema was already Epic-ready. **ADR-0014** records pure-Python-over-`legendary` (a deliberate deviation from the Phase-0 Manifesto wording).

- New `src/orchestrator/platform/epic/` package: `models.py` (dataclasses); `manifest.py` (binary-manifest parser ported from the spike — magic `0x44BEC00C`, zlib body, FString/GUID/chunk-data-list, version-aware `chunk_path` with ChunksV5/base64 for v≥22 and legacy hex; raises `EpicManifestError`, bounds `chunk_count`); `oauth.py` (authorization-code + refresh grants, refresh-token persisted 0600 at `epic_session_path`); `library.py` (paginated enumeration); `client.py` (`EpicClient` — token lifecycle facade, threaded via `Deps.epic_client`).
- New `src/orchestrator/prefill/epic_downloader.py` — async httpx, `Host`-header CDN routing + Epic UA, `Semaphore`, stream+discard, `[1,4,16]s` retry (4xx not retried), plus `verify_cached` (sample `X-Upstream-Cache-Status: HIT`).
- Handler Epic branches (dispatch on `job.platform`): `library_sync` (→ upsert `games`, `platform='epic'`); `prefill` (set `downloading` → **fresh** manifest fetch [Epic signed URLs expire] → store manifest + `size_bytes` → download → sample HIT-verify → `up_to_date`; any failed chunk → `failed`). Since F7-Epic disk-stat is deferred, the inline HIT verification is the validation — no separate validate job for Epic.
- New `POST/GET /api/v1/platforms/epic/auth` (submit `legendary.gl/epiclogin` code → exchange + persist + auto-enqueue `library_sync`; never echoes tokens) and `POST /api/v1/platforms/epic/library/sync` (parallel to the Steam route; per-platform dedup via `idx_jobs_library_sync_inflight`).
- Settings: `epic_token_url`, `epic_library_url`, `epic_manifest_url_template`, `epic_client_id`/`epic_client_secret` (the public legendary launcher creds), `epic_user_agent`, `epic_manifest_label`, `epic_platform`; reuses `epic_session_path`/`epic_refresh_buffer_sec`/`chunk_concurrency`/`prefill_chunk_*`.
- `validator/cache_key.epic_chunk_uri()` is **staged but unwired** — the F7-Epic disk-stat validator is a deferred follow-up (the Epic on-disk cache-key can only be derived from real cached chunks, post-live-UAT).
- **Hardened after a 4-lens adversarial review of the batch:** manifest download is now bounded by `manifest_size_cap_bytes` (was an OOM vector); the parser bounds `prereq_count` + validates `meta_size` (DoS guards); `fetch_manifest` validates the response-supplied CDN host (FQDN) + rejects `..` in the CDN base (SSRF/traversal); the refresh-token file opens with `O_NOFOLLOW` (symlink TOCTOU); `EpicClient` refreshes a near-expiry access token (`epic_refresh_buffer_sec`); and `_epic_prefill` marks the game `failed` on any error rather than leaving it stuck `downloading`. The review also caught two untested parser paths (zlib-compressed body, UTF-16 FString) — now covered.
- ~44 new tests (binary parser golden vectors incl. v22-base64 + legacy-hex + **zlib-compressed** + **UTF-16** + malformed/size-cap/bad-host; OAuth exchange/refresh/persist; paginated library; manifest fetch; EpicClient token lifecycle + expiry-refresh; downloader Host/path/retry/4xx/HIT; handler Epic branches + failure-marks-failed; auth + sync routers). Full suite: **1038 pass**. ruff/mypy(strict)/gitleaks clean. Security audit: 0 open findings (`docs/security-audits/f6-epic-prefill-security-audit.md`). **Live Epic UAT** (real account: deploy, paste auth code, prefill a title, confirm HIT) is the manual stopping point — analogous to F5's Steam 2FA.

### Fixed — SEV-4 backlog remediation (code review 2026-06-02) — 2026-06-02

The five SEV-4 findings from the 2026-06-02 review, batched.

- **Stability — `/health` false 503 from a busy reader:** `Pool.health_check()` probed every reader in `_reader_pool`, including checked-out ones. A reader busy with a slow real query would queue the `SELECT 1` probe behind it; the 1 s `wait_for` then timed out and marked the reader unhealthy — and `/health` requires `readers.healthy == readers.total`, so a single busy reader forced a spurious 503. It now probes only **idle** readers and counts in-use readers healthy (they're serving traffic and the read path self-polices a genuinely broken one); it also avoids running a probe concurrently on a connection mid-stream. In-use readers are tracked explicitly via an `_inuse_readers` id-set maintained in `_checkout_reader` (no reliance on `asyncio.Queue` internals — hardened after the adversarial review of this fix).
- **Correctness — `acquire_writer` dangling transaction:** the raw writer escape hatch yielded a connection with no transaction control. A caller that opened a transaction and forgot to commit/rollback left it open; the **next** writer's `execute_write` would then commit those stray writes along with its own. `acquire_writer` now rolls back any open transaction on context exit (best-effort), and documents that callers should prefer `write_transaction()`/`execute_write()`.
- **Stability — `pool_busy_timeout_ms` floor:** the setting allowed `0`, which disables SQLite's busy wait so any write contention surfaces immediately as `WriteConflictError`. Floor raised to `100` ms (`Field(ge=100)`).
- **Security — settings token-redaction precision (and an alias regression caught in review):** the startup token-error scrubber matched `"token" in str(loc)` as a substring (over-broad). It now matches each error-`loc` element **exactly and case-insensitively** against the secret field's lookup names. The adversarial review of this very fix caught a **SEV-1**: pydantic puts the matched *alias* in the error `loc`, so a too-short token supplied via the `ORCH_TOKEN` env var has `loc=('ORCH_TOKEN',)` — a naive field-name-only match would have **missed it and leaked the raw token** (worse than the substring). `_SECRET_FIELD_NAMES` now includes both `orchestrator_token` and `orch_token`; verified empirically against the env-alias path.
- **Resource — `manifest_fetch` temp-file leak:** the worker writes every depot's compressed BLOB to its own temp file up front; the per-iteration `finally` only cleaned the entry being processed, so a size-cap raise (or a skipped malformed entry) leaked the unprocessed depots' temp files. The loop is now wrapped in a `try/finally` that unlinks all `raw_path`s on exit (idempotent; the prompt per-iteration cleanup is kept so disk frees as we go).
- Tests: +9 (busy-reader health stays healthy; `acquire_writer` rolls back a dangling txn that would otherwise bleed into the next write; busy_timeout `0` rejected / `100` accepted; token error scrubbed via both kwarg and `ORCH_TOKEN` env-alias paths vs non-secret field; size-cap raise cleans unprocessed temp files). The batch was adversarially re-reviewed (3 lenses) — which caught the SEV-1 alias regression, the `_inuse_readers` hardening, and the rollback log. Full suite: **994 pass + 3 slow**. ruff/mypy(strict)/gitleaks clean.

### Fixed — SEV-3 cluster remediation (code review 2026-06-02) — 2026-06-02

Five verified SEV-3 findings from the 2026-06-02 review, batched.

- **Data Model / Stability — `library_sync` dedup race (migration 0004):** cron `enqueue_library_sync` and `POST /…/library/sync` both did an app-level SELECT-then-INSERT that straddled an await, so concurrent triggers could insert duplicate in-flight `library_sync` rows (the existing `idx_jobs_dedupe` is non-unique). Migration 0004 adds a **partial UNIQUE index** `idx_jobs_library_sync_inflight ON jobs(platform) WHERE kind='library_sync' AND state IN ('queued','running')` (with a one-time cleanup that cancels all-but-earliest pre-existing duplicates so it applies to deployed DBs). Both call sites now `INSERT … ON CONFLICT DO NOTHING`; `enqueue_library_sync` returns the real `rowcount` (was a hardcoded `return 1`); the sync endpoint returns the single in-flight job's id whether it inserted or deduped.
- **Stability — heartbeat `invalidate()` race:** `LancacheProbe.invalidate()` nulled the cache timestamp outside the refresh lock, so an in-flight `_refresh()` wrote a fresh timestamp on completion and swallowed the operator's forced refresh for a full TTL. Replaced with a one-shot `_force_refresh` flag, cleared at refresh **start**, so an invalidate arriving mid-refresh still forces the next probe.
- **Correctness — logging reserved-key protection:** `_protect_reserved_keys` only rescued `correlation_id` (contextvars-owned); user kwargs named `level`/`timestamp` were silently overwritten by the downstream `add_log_level`/`TimeStamper` processors. They are now rescued to `user_level`/`user_timestamp` (numbered-slot collisions handled). `RESERVED_KEYS` trimmed to the keys actually protected (`{correlation_id, level, timestamp, event}`) — `logger`/`logger_name` were never auto-added in this chain.
- **Security — `build_order_by_clause` ORDER BY injection footgun:** `allow_list` was optional and field re-validation was skipped when omitted (asymmetric with `build_where_clause`) — a latent ORDER BY injection for any hand-built `SortField` caller. `allow_list` is now **required**, and `direction` is validated against `{asc, desc}` too (the `Literal` type isn't enforced at runtime — an adversarial review of this fix found the `direction` half of the same footgun still open). All three routers already pass `allow_list` and build sort specs via `parse_sort`, so neither half was reachable in practice.
- Tests: +~24 (dedup index enforcement + concurrent enqueue + migration-cleanup; heartbeat mid-flight-invalidate determinism; level/timestamp rescue + numbered-slot chain + accurate `RESERVED_KEYS`; order-by required-allow_list + field/direction rejection; sync 503 retry path). Reaper/worker fixtures switched off `library_sync` for multi-in-flight scenarios (now singleton-per-platform). The batch was adversarially re-reviewed (4 lenses) before commit. Full suite: **984 pass**. ruff/mypy(strict)/gitleaks clean.

### Fixed — Security/Stability: DB pool reader-exhaustion deadlock (SEV-2) — 2026-06-02

Surfaced by the multi-agent code review. On reader I/O errors (e.g. a failing DB volume), `_replace_connection`'s storm-guard and replacement-open-failure paths returned **without re-queueing** the reader, permanently shrinking the reader pool; with `_checkout_reader` awaiting `self._readers.get()` with **no timeout**, once the queue drained **every read blocked forever** while the pool still reported `state="ready"` and raised no `PoolError`. The existing chaos/slow tests masked it (they asserted only a health symptom, never the loud-failure contract, and never issued the follow-up read that would hang).

- **Bounded reader acquire** (`db/pool.py` `_acquire_reader`): reads now wait at most `pool_reader_acquire_timeout_sec` (default 30 s) for a free reader; on exhaustion they raise `PoolError` (→ 503) instead of hanging — fail loud, not silent.
- **Lost-slot tracking + heal-on-exhaustion:** a give-up replacement now records the lost slot (`_lost_reader_slots`) instead of silently leaking it; on an acquire timeout with a recorded deficit the pool opens a fresh reader, so capacity **recovers automatically once the fault clears** (rather than requiring a restart).
- New setting `pool_reader_acquire_timeout_sec` (default 30.0, range 0<t≤300).
- New regression tests `tests/db/test_pool_reader_exhaustion.py` (exhaustion → `PoolError` within bounded wall-clock, not a hang; recovery after the fault clears). Strengthened the two masking storm tests to assert the lost-slot contract. Full suite: 966 pass + 3 slow.

### Fixed — Stability: scheduler `start()` no longer leaks a stale instance (SEV-2) — 2026-06-02

From the 2026-06-02 code review. `SchedulerManager.start()` guarded idempotency with `if self._scheduler is not None and self._scheduler.running: return`. On the **non-running** path — a held-but-stopped scheduler, e.g. after a prior `shutdown()` that raised partway and left `_scheduler` dangling — `start()` fell through and **overwrote** `self._scheduler` with a fresh `AsyncIOScheduler`, silently abandoning the prior object instead of disposing of it. The "idempotent start" contract was false on that path. Currently unreachable via the single-call lifespan, but a latent lifecycle bug.

- `start()` now disposes any held-but-stopped scheduler (warning `scheduler.replacing_stale_instance` + new best-effort `_dispose_stale_scheduler()`, which clears the reference and stops the instance if it is somehow still running) **before** building a fresh one — the manager never abandons a scheduler it owns.
- Hardened per an adversarial review of the fix: `start()`/`shutdown()` are now serialized behind an `asyncio.Lock`, and `_dispose_stale_scheduler()` is synchronous, so the rebuild is atomic and a future concurrent caller (e.g. a restart endpoint) can never race to construct two schedulers. Uncontended acquire does not suspend, so the single sequential lifespan path is unchanged. (The reviewed TOCTOU/`wakeup`-after-`_eventloop=None` races were shown empirically to be unreachable in the current code; the lock makes the atomicity explicit and durable rather than resting on a subtle no-await invariant.)
- +3 regression tests (`tests/scheduler/test_manager.py`): replacing a stale non-running instance emits the warning, runs the dispose path, and swaps cleanly (no silent leak); end-to-end recovery after a dangling scheduler yields a fully functional, job-registered scheduler; and two concurrent `start()` calls after a dangling scheduler converge on exactly one running instance. Tests use `structlog.testing.CapturingLogger` patched onto the module logger (the app sets `cache_logger_on_first_use=True`, which defeats `capture_logs` once any earlier test caches the bound logger) and poll for APScheduler's deferred shutdown to keep the precondition deterministic. Full suite: 966 pass. ruff/mypy(strict, `src/`)/gitleaks clean. Security audit: 0 findings (`docs/security-audits/scheduler-start-leak-fix-security-audit.md`).

### Added — F5 Steam CDN Prefill — 2026-05-29

Implements F5 from PROJECT_BIBLE §1.2 — downloads a Steam game's depot chunks **through** the lancache so they get cached. Together with F7 this closes the orchestrator's core loop (prefill → cache → validate). Steam-only; F6 (Epic) deferred.

- New `src/orchestrator/prefill/downloader.py` — async httpx engine. For each chunk, `GET {lancache_base_url}/depot/{depot_id}/chunk/{sha}` with `User-Agent: Valve/Steam HTTP Client 1.0` + `Host: lancache.steamcontent.com`, **streamed and discarded**, so lancache caches it under the exact key F7 validates (verified live, spike A5). Bounded by `Semaphore(chunk_concurrency)`; per-chunk read timeout + retry/backoff (`[1,4,16]s`, 4xx not retried). Chunk URLs are unauthenticated — no manifest request code needed.
- New `src/orchestrator/jobs/handlers/prefill.py` — `prefill` job handler: sets `games.status='downloading'`, builds the deduped `(depot_id, sha)` chunk list (latest manifest per depot → worker `manifest.expand`, reusing F7's path; fetches manifests first if the game has none), downloads, and on full success **enqueues a `validate` job (ID5)**. Any failed chunk → `games.status='failed'` + job failed.
- New `POST /api/v1/games/{game_id}/prefill` (`api/routers/prefill_trigger.py`) — bearer-gated, in-flight dedup, 202/400/404/503.
- Settings: `lancache_base_url` (`http://127.0.0.1`), `steam_cdn_host` (`lancache.steamcontent.com`), `prefill_user_agent`, `prefill_chunk_timeout_sec` (10), `prefill_chunk_max_attempts` (3); reuses the pre-staged `chunk_concurrency` (32).
- ~25 new tests (downloader via httpx MockTransport: headers/path/retry/4xx/concurrency/progress; handler status transitions + ID5 enqueue + fetch-if-no-manifests; trigger 202/dedup/404/400/auth/503; settings bounds). Full suite: 963 pass. ruff/mypy/gitleaks/semgrep clean; security audit 0 SEV (`docs/security-audits/f5-steam-prefill-security-audit.md`).

### Fixed — F7 validator excludes unreadable (mode-000) cache files — 2026-05-29

Bundled with F5 (operator-approved). `disk_stat` now requires the owner-read bit (`st_mode & 0o400`) in addition to exists + size>0. ~1.7% of cache files on the host are mode-000 — they exist but lancache (`www-data`) can't read them (`Permission denied` → 500 → re-download), so they were being over-counted as cached. The check uses `stat()` only (no `open()`), so it works despite the orchestrator (uid 1000) not being able to read `www-data:600` files. Operational finding: issue #128.

### Fixed — UAT-9 hardening (BL12 + F7 agent sweep) — 2026-05-29

Remediation of the UAT-9 adversarial agent sweep over BL12 + F7 (pre-live-test hardening). Deferred SEV-3/4 items tracked in issues #121–#123.

- **Security/correctness (SEV-2):** `cache_levels` is now validated both at config load (`Settings`) and in `cache_path` — widths must each be ≥1 and total ≤32. A value like `"0"` or `"99"` previously passed the regex and silently produced wrong cache paths (validator reporting *everything missing* deployment-wide).
- **SEV-2 — large-manifest IPC (S2-1/S2-2):** `manifest.fetch` packed all depots into one IPC response line, overflowing the 10 MiB cap on 50+ depot games (worker killed → restart-storm); `manifest.expand` inflated a stored BLOB to a ~170 MB stdin line. Both now use a **temp-file handoff**: the producer writes the BLOB to a temp file on the shared container FS (`tempfile.gettempdir()`, uuid-named) and the IPC carries the path; the consumer reads then unlinks. Eliminates IPC-size limits for both ops.
- **SEV-3 — validator robustness:** a manifest that expands to zero chunks is now classified `cached` (up to date), not `error`; a malformed chunk SHA yields `outcome=error` instead of an uncaught crash that loses the whole job; `validate_game` asserts the worker-parsed `depot_id` matches the DB row (fails closed on mismatch).
- **SEV-3/4 polish:** D8 path-containment backstop in `cache_path`; `validate_chunks` no longer follows symlinks (a symlinked cache path is not a genuine chunk) and aggregates per-file stat errors into one WARN per run.
- Tests: +~15 (cache_levels bounds, 0-chunk, malformed-SHA, depot mismatch, symlink, temp-file handoff round-trips). Full suite: 928 pass. ruff/mypy/gitleaks/semgrep clean.

### Added — F7 Cache Validator (disk-stat) — 2026-05-28

Implements F7 from PROJECT_BIBLE §1.2 — the orchestrator's core value proposition. Determines whether a Steam game's depot-manifest chunks are present in the lancache on-disk cache by computing each chunk's nginx cache path and `os.stat`-ing it. Records a `validation_history` row and updates `games.status`. `/health.validator_healthy` is now real.

- New `src/orchestrator/validator/cache_key.py` — pure, offline cache-key derivation. The nginx key is `md5(identifier + uri + slice_range)`; the on-disk path consumes hex from the END of the digest per `levels` (for `2:2`: `<H[-2:]>/<H[-4:-2]>/<H>`). **Verified empirically against the live lancache (spike A4), correcting two FRD errors: 10 MiB slice (not 1 MiB) and the levels directory ordering.** Validates `depot_id`/`sha_hex` shape (path-traversal guard).
- New `src/orchestrator/validator/disk_stat.py` — `validate_chunks` (batched `os.stat` via `run_in_executor`; cached = exists AND size>0, never size-match) + `validate_game` (latest manifest per depot, dedup chunk SHAs, classify cached/partial/missing/error).
- New worker IPC op `manifest.expand` (`platform/steam/worker.py`) — **offline** zstd-decompress + `DepotManifest(data)` parse in the worker venv (ADR-0013 D14), returning `{depot_id, chunk_shas}`. No Steam session required, so validation works even with expired auth. Plus `client.manifest_expand()` + per-op timeout.
- New `src/orchestrator/jobs/handlers/validate.py` — `validate_handler`: writes `validation_history` (`method='disk_stat'`) and maps outcome → `games.status` (cached→`up_to_date`, partial/missing→`validation_failed`; **`error` never clobbers status**).
- New `POST /api/v1/games/{game_id}/validate` (`api/routers/validate_trigger.py`) — bearer-gated, in-flight dedup, 202/400/404/503.
- New `src/orchestrator/validator/self_test.py` — startup self-test (cache root is a listable dir + key-derivation smoke) wires `app.state.validator_healthy`; `/health` now includes it in the `all_healthy` conjunction (was a BL5 stub-false).
- Settings: `steam_cache_identifier` (default `steam`) + `steam_worker_manifest_expand_timeout_sec` (default 120, 30..600).
- ~50 new tests (cache_key golden vectors incl. 3 real cached chunks, disk_stat engine, validate handler, trigger router, self-test, health gating, settings, migration). Full suite: 919 pass. ruff/mypy/gitleaks/semgrep clean; security audit 0 SEV (`docs/security-audits/f7-cache-validator-security-audit.md`).

### Data Model — `manifests.depot_id` (migration 0003) — 2026-05-28

Adds a nullable `depot_id INTEGER` column to `manifests` plus index `idx_manifests_game_depot(game_id, depot_id, fetched_at DESC)`. F7 needs `depot_id` to build chunk URLs (`/depot/<depot_id>/chunk/<sha>`) and to select the latest manifest per depot. The BL12 `manifest_fetch` handler now populates it (the depot id was already in the worker IPC payload). Forward-only, nullable `ADD COLUMN` (STRICT-safe, no table rebuild); no backfill needed (no live manifest data exists yet). CHECKSUMS manifest pinned.

### Added — BL12 Steam Manifest Fetcher (F1 milestone 3/3) — 2026-05-28

Implements BL12 from PROJECT_BIBLE §1.2 MVP cutline — the final F1 milestone. Fetches the operator's owned depot manifests for a single Steam app via the worker subprocess's Steam CDN client and upserts the `manifests` table, recording version, chunk count, total bytes, and the raw compressed manifest BLOB. Unblocks the F7 validator (which deserializes the stored BLOB inside the worker venv).

- New `manifest_fetch` job kind. Migration [`0002_jobs_kind_manifest_fetch.sql`](src/orchestrator/db/migrations/0002_jobs_kind_manifest_fetch.sql) extends the `jobs.kind` CHECK constraint (SQLite can't ALTER a CHECK in place — uses the snapshot→drop→recreate→restore recipe with a regex-clean expected-tables set).
- New [`src/orchestrator/jobs/handlers/manifest_fetch.py`](src/orchestrator/jobs/handlers/manifest_fetch.py) — `manifest_fetch_handler`. Looks up `app_id` from `games`, calls `steam_client.manifest_fetch(app_id)`, UPSERTs each returned depot manifest `ON CONFLICT(game_id, version)`, and sets `games.size_bytes` to the SUM of manifest `total_bytes` (full install size across depots). Re-fetch of the same `manifest_gid` is idempotent; a new gid adds a historical row.
- New worker IPC op `manifest.fetch` in [`src/orchestrator/platform/steam/worker.py`](src/orchestrator/platform/steam/worker.py) — lazily constructs a `CDNClient`, calls `cdn.get_manifests(app_id, branch="public")`, and returns `{manifests: [{depot_id, manifest_gid, name, total_bytes, chunk_count, raw_b64}, ...]}`. Per spike-A3, the raw manifest is serialized via protobuf `.serialize()` (NOT pickle — `CDNDepotManifest` holds an unpicklable `cdn_client` back-reference), then zstd-compressed and base64-encoded.
- New [`src/orchestrator/api/routers/manifest_trigger.py`](src/orchestrator/api/routers/manifest_trigger.py) — `POST /api/v1/games/{game_id}/manifest/fetch`. Handler-side dedup of in-flight jobs (race-tolerant per P8); 202 + `job_id`, 400 non-steam, 404 unknown game, 503 on `PoolError`.
- `client.manifest_fetch(app_id)` method + per-op `manifest.fetch` timeout override on `SteamWorkerClient`.
- `NotAuthenticated` from the worker flips `platforms.auth_status='expired'` before re-raising (mirrors the BL11 F-UAT6-3 fix), so an expired session surfaces in `/platforms`.
- Anomaly guard: a single manifest BLOB exceeding `Settings.manifest_size_cap_bytes` (default 128 MiB) raises rather than storing — defends against a malformed/hostile CDN response.
- 35 new tests: 13 handler (`tests/jobs/test_manifest_fetch_handler.py`), 9 router (`tests/api/test_manifest_trigger_router.py`), settings bounds (`tests/core/test_settings.py`), client IPC round-trip (`tests/platform/steam/test_client_unit.py`), migration runner coverage. Full suite: 874 pass.

### Data Model — `jobs.kind` adds `manifest_fetch` (migration 0002) — 2026-05-28

`jobs.kind` CHECK constraint extended from `('prefill','validate','library_sync','auth_refresh','sweep')` to additionally allow `'manifest_fetch'`. Forward-only; the recipe preserves all existing rows and recreates the four `idx_jobs_*` indexes identically to 0001. Rollback: restore from backup (no down-migration — STRICT table CHECK changes are not reversible without a data round-trip). CHECKSUMS manifest pinned for supply-chain tamper detection.

### Added — F10 Status Page — 2026-05-28

Implements F10 from PROJECT_BIBLE §1.2 + §9.3. Single-file HTML status dashboard at `GET /`. Operator-facing summary of system state — Health, Platforms, Active Jobs, Library Stats, Recent Errors — polled from the existing `/api/v1/*` endpoints.

- New [`src/orchestrator/api/routers/status.py`](src/orchestrator/api/routers/status.py) — embeds a single-file HTML+CSS+vanilla-JS page. Self-contained: no external dependencies (works offline on LAN-only deployments).
- `GET /` is auth-exempt at the page-fetch level (Bible §9.3) — the embedded JS prompts the operator for the bearer token at first load and persists it in `sessionStorage` for subsequent `/api/v1/*` calls. The data endpoints themselves remain auth-gated by `BearerAuthMiddleware`.
- Accessibility (Intake §9 colorblind constraint): every status indicator combines **color + ASCII icon + text label**. Text label is the hard constraint — survives even with color stripped.
- Polling: `/health`, `/platforms`, `/jobs` (queued + running) every 2 s; `/games`, `/manifests`, `/jobs?state=failed` every 10 s. Backs off to 10 s on 5xx until success.
- Security headers: `Cache-Control: no-store`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. `<meta name="robots" content="noindex,nofollow">`.
- Bundle size: ~14 KB raw, ~5.8 KB gzipped — well under the Bible §9.3 < 20 KB ceiling.
- 18 new tests: route returns 200 + text/html, security headers, auth exemption, panel/pill IDs present, accessibility text labels, size invariants, no-external-deps regex check, endpoint references for drift detection.

### Added — F12 Scheduler subsystem — 2026-05-28

Implements F12 from PROJECT_BIBLE §1.2 MVP cutline and JQ3 (`/health.scheduler_running` is now real). APScheduler 3.11.2 `AsyncIOScheduler` integrated into FastAPI lifespan; periodically enqueues `library_sync` jobs for the BL11 jobs worker to execute.

- New `src/orchestrator/scheduler/` package with `SchedulerManager` (wraps `AsyncIOScheduler`) + `enqueue_library_sync` cron callback. `replace_existing=True` jobs registered at boot; in-memory `MemoryJobStore` (default) means cron config re-renders on every restart.
- Settings:
  - `scheduler_enabled: bool = True` — diagnostic / dev escape hatch
  - `scheduler_library_sync_interval_sec: int = 21600` (6h, range 60..86400)
- Lifespan ordering: scheduler starts at step 4b (between jobs worker spawn and lancache probe init); shuts down FIRST in teardown so it can't enqueue work during shutdown.
- `/health.scheduler_running` reads `app.state.scheduler_manager.running`. No-lifespan test fixtures fall back to `False` via `getattr(..., None)` guard.
- Cron callbacks include dedup against existing queued/running library_sync rows (mirrors the manual sync endpoint) so cron + operator triggers don't race onto duplicate rows.
- 25 new tests: 7 in `tests/scheduler/test_jobs.py` (enqueue + dedup + error swallow), 7 in `tests/scheduler/test_manager.py` (start/stop/running/jobs), 3 in `tests/api/test_lifespan_scheduler.py` (integration), 5 in `tests/core/test_settings.py` (defaults + bounds), 3 in `tests/api/test_health_endpoint.py` (probe wiring). Full suite: 830 pass.
- Updated `tests/api/test_lifespan.py::test_lifespan_returns_503_through_handler_when_unhealthy` — `scheduler_running` is now `True` post-F12; `validator_healthy` is the remaining stub-false subsystem keeping /health at 503.

### Fixed — ID2 lancache probe: real lancache returns 204 with header identifier — 2026-05-28

Surfaced by post-PR-#113 deployment testing against the running `lancachenet/monolithic` image: lancache's `/lancache-heartbeat` endpoint returns **HTTP 204 No Content** (not 200) and identifies itself via the **`X-LanCache-Processed-By`** response header. PR #113's `LancacheProbe._refresh()` strictly checked for 200 → would have reported `lancache_reachable: false` even against a healthy lancache.

- `LancacheProbe` now accepts any 2xx status code AND requires the `X-LanCache-Processed-By` header. The header check is positive identification — defends against misconfigured DNS bypass / wrong target where some other 2xx-responding service would otherwise pass the probe.
- Headers checked case-insensitively (httpx normalizes lookup).
- New structured log event: `lancache.probe.missing_identifier_header` (WARN) fires when a 2xx response arrives without the lancache header — helps operators diagnose "responsive endpoint, wrong target."
- 5 new tests in `tests/lancache/test_heartbeat.py` (204 with header, 200 with header, all-2xx-with-header, 2xx-without-header-rejected, case-insensitive header match). Renamed `test_non_200_returns_false` → `test_non_2xx_returns_false`.

### Added — ID6 Startup Job Reaper — 2026-05-27

Implements `docs/phase-0/frd.md:649` ID6 — the "startup reaper for abandoned jobs" requirement. Closes the SEV-3 deployment-shape finding F-UAT6-8 (stale `library_sync` jobs surviving container restarts forever).

- New `src/orchestrator/jobs/reaper.py` with `reap_running_jobs(pool) -> int`. Single atomic `UPDATE jobs SET state='failed', error=..., finished_at=CURRENT_TIMESTAMP WHERE state='running'`.
- FastAPI lifespan calls the reaper after pool init but before the jobs worker task spawns — orphans get cleaned before the new worker could conceivably mis-claim them. Defensive try/except around the call so a failed reap doesn't abort boot.
- Tests: 7 unit tests in `tests/jobs/test_reaper.py` (empty table, no-running, single, multiple, mixed-states, idempotency, error-message length contract). 3 integration tests in `tests/api/test_lifespan_reaper.py` driving the real lifespan via `asgi_lifespan.LifespanManager` — seeds an orphaned `running` job pre-boot, verifies it's flipped to `failed` post-boot.

### Added — ID2 Lancache self-test — 2026-05-27

Implements the ID2 implicit-dependency feature from `docs/phase-0/frd.md:645` and `docs/phase-1/architecture-proposal.md` — the operator-facing `/api/v1/health` endpoint now surfaces a real `lancache_reachable` boolean derived from an HTTP probe to `<lancache>/lancache-heartbeat`, replacing the BL5 stub-false.

- New `src/orchestrator/lancache/` package with `LancacheProbe` (async, cache-TTL'd, concurrency-safe via `asyncio.Lock`). 16 tests cover happy path, every documented httpx failure mode, TTL cache hit/miss/refresh, concurrent-probe collapse, and URL validation.
- New Settings fields:
  - `lancache_heartbeat_url` (default `http://lancache/lancache-heartbeat`)
  - `lancache_probe_timeout_sec` (default 5.0, range 0–60)
  - `lancache_probe_cache_ttl_sec` (default 30.0, range 0–600)
- FastAPI lifespan startup constructs the singleton probe and stashes it on `app.state.lancache_probe`. The `/health` router calls `await probe.probe()` per request (cache-fast — usually no IO).
- Tests in `tests/api/test_health_endpoint.py` verify the wiring through stub probes; the no-lifespan `unit_app` fixture falls back to `False` instead of crashing.

### Fixed — Post-UAT-6 SEV-2 batch — 2026-05-27

Closes #107 (licenses enumeration) and #109 (`get_product_info` timeout) — both surfaced by the UAT-6 live operator session against a real Steam account.

- **#107** — Worker library enumeration extracted from `worker.py` into a pure, unit-testable `enumerate.py` module:
  - `wait_for_licenses(client, timeout=10s)` polls `client.licenses` (which is `dict[int, License]`, populated asynchronously by `EMsg.ClientLicenseList`) until non-empty or deadline
  - `enumerate_apps(client, batch_size=50)` iterates `licenses.values()` (the previous code iterated the dict yielding keys, then `getattr(int, "package_id")` returned None — explaining the "0 apps for every real account" symptom)
  - Skips `auto_access_tokens=True` for the package call — `licenses[pid].access_token` is already known, saving one Steam round-trip per batch
- **#109** — Chunks `get_product_info(packages=...)` and `get_product_info(apps=...)` into batches of 50. New `Settings.steam_worker_library_enumerate_timeout_sec` (default 300, range 30–3600) drives a per-op timeout override in `SteamWorkerClient._send_and_await` so library_enumerate gets a 5-minute budget while other ops keep the 30s default.

Test coverage: 30 new tests in `tests/platform/steam/test_enumerate.py` covering the chunking, wait, build-package-request, extract-app-ids, extract-app-metadata, and end-to-end enumeration paths; 3 new tests in `tests/platform/steam/test_client_unit.py` for the per-op timeout override. Full suite: 760 tests pass.

### Documentation

- New `spikes/spike_a2_steam_modern.md` — full steam-next 1.4.4 API investigation documenting the actual licenses/get_product_info/login surfaces that BL10/BL11 had wrong.
- New `docs/known-limitations.md` — operator-facing note explaining the steam-next-driven container-restart re-auth requirement.
- Closed #108 (session persistence) with detailed won't-fix-at-current-scope rationale referencing the spike doc. Opened strategic follow-up #111 for future Steam-library evaluation.

### Security — UAT-6 SEV-2 remediation — 2026-05-26

Three production-blocking findings from the UAT-6 agent sweep, all fixed test-first:

- **F-UAT6-1 [SEV-2]** (`src/orchestrator/platform/steam/client.py`) — `SteamWorkerClient.start()` now passes `limit=MAX_IPC_LINE_BYTES + 1 KiB` to `asyncio.create_subprocess_exec`, sizing the StreamReader's internal buffer above asyncio's default 64 KiB. Pre-fix, any Steam library response > 64 KiB (true for any account with ~600+ apps) would have crashed the reader task on a raw `ValueError` from `readline()`, leaked the worker subprocess, and prevented the restart-storm guard from firing. The `_read_loop` now also catches `ValueError` and `LimitOverrunError`, emitting `steam_worker.ipc_response_overflow` and calling `_on_worker_died(reason='response_too_large')`.
- **F-UAT6-2 [SEV-2]** (`src/orchestrator/platform/steam/{client,worker}.py`) — Worker now reads its credential-location directory from `os.environ["ORCH_STEAM_SESSION_DIR"]` (falling back to the historical default) instead of the hardcoded `/var/lib/orchestrator/steam_session`. `SteamWorkerClient.start()` forwards `Settings.steam_session_dir` into the subprocess env. Pre-fix, operators with a customized volume mount silently lost refresh-token persistence across restarts.
- **F-UAT6-3 [SEV-2]** (`src/orchestrator/jobs/handlers/library_sync.py`) — `library_sync_handler` now catches `SteamWorkerError(kind='NotAuthenticated')`, updates `platforms.auth_status='expired'` and `last_error`, then re-raises so the job is still marked failed. Other `SteamWorkerError` kinds (e.g. `SteamAPIError`) leave `auth_status` unchanged — those represent transient failures, not session expiry. Pre-fix, `GET /platforms` would show `auth_status='ok'` while `GET /platforms/steam/auth/status` simultaneously returned `authenticated=false`.

### Added — BL11 Steam Library Sync (F1 milestone 2/3) — 2026-05-25
- `src/orchestrator/jobs/` package — generic asyncio job dispatcher
  (`worker.py`, `handlers/__init__.py` registry). Single-loop topology
  (spec D10) with atomic SELECT-then-UPDATE claim under `BEGIN IMMEDIATE`
  so concurrent claims serialize.
- `library_sync` handler (`src/orchestrator/jobs/handlers/library_sync.py`)
  calls `library.enumerate` on the steam worker subprocess and upserts the
  `games` table via `INSERT ... ON CONFLICT(platform, app_id) DO UPDATE` —
  re-sync is idempotent; downstream lifecycle columns (status,
  cached_version, last_validated_at) are preserved.
- `POST /api/v1/platforms/steam/library/sync` — manual sync trigger with
  handler-side dedup of queued/running jobs (existing in-flight job_id
  returned instead of creating a duplicate).
- `library.enumerate` IPC op on the steam worker subprocess. Walks
  `_client.licenses` → `get_product_info(packages=...)` → `get_product_info(apps=...)`
  to assemble owned-app metadata; live Steam validation deferred to UAT-6.
- `SteamWorkerClient.library_enumerate()` async method.
- Auto-queue `library_sync` job after BOTH Steam auth-success paths
  (no-2FA and 2FA), best-effort — DB failure during enqueue is logged
  but does NOT fail the auth response.

### Changed — BL11
- FastAPI lifespan now spawns the jobs worker asyncio task at startup
  and cleanly stops it (5 s shutdown timeout, then cancel) ahead of
  steam-client + pool shutdown.

### Infrastructure — BL11
- Settings field `jobs_worker_poll_interval_sec` (range 0.05–60.0,
  default 1.0) governs the empty-queue poll cadence.

### Documentation — BL11
- New BL11 feature entry in `FEATURES.md`.
- Plan: `docs/superpowers/plans/2026-05-25-bl11-library-sync.md`.

### Security
- **UAT-5 remediation (7 findings)** hardening the BL5-BL9 API surface:
  - **U5-1 [SEV-2]** (`middleware.py`) — bearer-auth Authorization header now
    decoded with `errors="strict"` (was `errors="ignore"`, which silently
    dropped non-ASCII bytes); added 4096-byte header-size cap. Non-conforming
    HTTP clients can no longer send byte sequences that decode to the same
    token via silent normalization.
  - **U5-2 [SEV-2]** (`routers/games.py`, `routers/jobs.py`, `routers/platforms.py`) —
    per-row response-model construction wrapped in `try/except ValidationError`.
    Out-of-Literal DB values (CHECK-constraint drift, raw SQL writes) now drop
    the offending row with a structured `api.{entity}.row_dropped` log instead
    of crashing the whole request to 500.
  - **U5-3 [SEV-2]** (`routers/games.py`, `routers/jobs.py`) — defensive
    `isinstance(raw_meta, (str, bytes, bytearray))` guard before `len()` on
    metadata/payload bytes. Future pool drivers that return non-buffer types
    (dict, int) no longer raise unhandled TypeError to 500.
  - **U5-4 [SEV-2]** (`_query_helpers.py`) — `_coerce_value` rejects
    non-finite floats (`NaN`, `Infinity`, `-Infinity`). Previously these
    flowed through to `json.dumps` and crashed to 500; now they 400 with a
    clear `value must be finite` message.
  - **U5-5 [SEV-2]** (`routers/platforms.py`) — platforms now rejects any
    query parameter with 400 for cross-router consistency. Previously
    `?password=foo` silently returned 200 (the other 3 F9 endpoints all 400).
  - **U5-6 [SEV-2]** (`routers/platforms.py`) — added `PlatformsMeta` to the
    response envelope (`{platforms, meta}`). Envelope shape now matches
    games/jobs/manifests; meta carries `total` plus empty
    `applied_filters`/`applied_sort` (platforms doesn't paginate or filter).
  - **U5-8 [SEV-3]** (`routers/games.py`, `routers/jobs.py`) — both routers
    declare an empty `IncludeAllowList` and call `parse_includes`. Any
    `?include=foo` value now rejects with 400; previously silently ignored.
    Locks in the BL9 convention so future typos surface.

  See [UAT-5 session](tests/uat/sessions/2026-05-20-session-5/) for full
  consolidated findings + 4 individual + 2 umbrella issues filed (#78-#87).

### Added
- **BL10 — Steam authentication substrate** (F1 milestone, BL10/3). First
  real data-ingestion feature substrate. Subprocess-isolated steam-next
  worker (gevent-patched, separate venv) communicates with the asyncio
  orchestrator via newline-delimited JSON over stdin/stdout pipes.
  New endpoints:
  - `POST /api/v1/platforms/steam/auth` (loopback-only) — initiates
    Steam login; returns `200` (no 2FA) or `202 + challenge_id` (2FA
    required).
  - `POST /api/v1/platforms/steam/auth/{challenge_id}` (loopback-only) —
    completes 2FA with a code; 5-min TTL on challenges.
  - `GET /api/v1/platforms/steam/auth/status` (bearer; NOT loopback-only;
    Game_shelf reads it).

  Session persistence: steam-next manages its own credential dir at
  `/var/lib/orchestrator/steam_session/` (mode 0700); the orchestrator
  writes a metadata JSON at `/var/lib/orchestrator/steam_session.json`
  (mode 0600) — NEVER contains tokens, only `{steam_id, username,
  last_refreshed_at, sha256_prefix, auth_method_version}`. Atomic write
  via `os.replace` from a tempfile.

  `platforms` table updates: `auth_status` transitions `never → ok` or
  `→ error`; `last_sync_at` updated on success; `last_error` populated
  on failure (truncated to 200 chars); `config` JSON has `{steam_id,
  username, last_refreshed_at}` — NEVER tokens (D12).

  Settings additions: `steam_worker_python_path`,
  `steam_worker_ipc_timeout_sec` (default 30), `steam_worker_max_restart_attempts`
  (default 3), `steam_session_dir`, `jobs_worker_poll_interval_sec`
  (used in BL11; pinned here for venv-shape stability).

  See [F1 spec](docs/superpowers/specs/2026-05-24-f1-steam-credentials-fetcher-design.md)
  and [ADR-0013](docs/ADR%20documentation/0013-steam-subprocess-isolation.md)
  for full architecture.
- **`GET /api/v1/manifests`** (BL9 / Feature 9 partial) — third paginated F9
  read endpoint, introduces the **`?include=` opt-in expansion convention**.
  Default sort `fetched_at:desc` (matches `idx_manifests_game_fetched`).
  Per-endpoint filterable: `game_id` (eq, `_in`), `version` (eq, `_in`),
  `fetched_at` (range), `chunk_count` (range), `total_bytes` (range).
  Sortable: `id`, `game_id`, `version`, `fetched_at`, `chunk_count`,
  `total_bytes`. `raw` BLOB column intentionally excluded. With
  `?include=game`, the response embeds a `game: {title, platform, app_id}`
  summary via a follow-up `WHERE id IN (...)` games lookup keyed by the
  distinct game_ids on the page (switched from the LEFT JOIN spec'd in D7
  to avoid an ambiguous-`id` issue with the unqualified ORDER BY tie-breaker
  — same wire behavior, cleaner SQL). Adds `IncludeAllowList` +
  `parse_includes` to `_query_helpers.py` (+~30 LoC, identifier-validated
  + `"include"` reserved) — future endpoints can opt-in to FK expansion
  cheaply. See
  [spec](docs/superpowers/specs/2026-05-20-bl9-manifests-readonly-design.md)
  and [audit](docs/security-audits/bl9-f9-manifests-readonly-security-audit.md).
- **`GET /api/v1/jobs`** (BL8 / Feature 9 partial) — second paginated F9
  read endpoint. Returns the orchestrator jobs feed with filter, sort,
  and pagination. Default sort `id:desc` (most-recently-created first);
  active jobs surface via `?state_in=queued,running`. Per-endpoint
  filterable: `kind`, `game_id`, `platform`, `state`, `progress` (range),
  `source`, `started_at`/`finished_at` (range). Sortable: `id`, `kind`,
  `state`, `progress`, `started_at`, `finished_at`. `payload` JSON column
  included as parsed dict (UAT-4 hardening: 64 KiB cap + RecursionError
  catch + null on parse failure); `error` truncated to 200 chars.
  Validates the proposition that BL7+UAT-4-hardened `_query_helpers.py`
  conventions propagate cheaply — **zero changes to the shared module**.
  See [spec](docs/superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md)
  and [audit](docs/security-audits/bl8-f9-jobs-readonly-security-audit.md).
- **`GET /api/v1/games`** (BL7 / Feature 9 partial) — first paginated F9
  read endpoint. Returns the games library with filter (operator-suffix
  syntax: `field`, `field_in`, `field_gte`, `field_lte`), sort (multi-field
  with `:asc`/`:desc` + server-appended `id:asc` tie-breaker with
  de-duplication), and offset-based pagination (default 50, max 500,
  reject 400 above max). Rich meta envelope: `total`, `limit`, `offset`,
  `has_more`, `applied_filters`, `applied_sort`. New shared module
  `src/orchestrator/api/_query_helpers.py` provides parser/validator/SQL
  builder primitives reusable by every future paginated F9 endpoint
  (`/jobs`, `/manifests`, etc.). `metadata` column included as parsed JSON
  (null on parse failure); `last_error` truncated to 200 chars (BL6
  pattern). Pool failures translate to 503 with structured
  `api.games.read_failed` log. SQL injection resistance pinned by both
  unit tests and a Hypothesis property test. See
  [spec](docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md)
  and [audit](docs/security-audits/bl7-f9-games-readonly-security-audit.md).
- **`GET /api/v1/platforms`** (BL6 / Feature 9 partial) — first real
  domain endpoint on the BL5 substrate. Returns the auth + sync status
  of every configured platform, with Steam pinned first in the response
  order. Six fields per platform (name, auth_status, auth_method,
  auth_expires_at, last_sync_at, last_error); `config` column
  intentionally excluded from the response surface. `last_error`
  truncated to 200 chars at the API layer (defense-in-depth on top of
  upstream redaction). Pool failures translate to HTTP 503 with a
  structured `api.platforms.read_failed` log event. Locks the wrapped
  envelope shape `{"<resource>": [...]}` that every future F9 read
  endpoint will inherit. See
  [spec](docs/superpowers/specs/2026-04-30-bl6-platforms-readonly-design.md)
  and [audit](docs/security-audits/bl6-f9-platforms-readonly-security-audit.md).
- **FastAPI app skeleton** (BL5 / Feature 5) — `create_app()` factory at
  `src/orchestrator/api/main.py`. Lifespan runs migrations + initializes
  the BL4 pool singleton on startup; closes the pool with the BL4 30 s
  hard timeout on shutdown. Run with
  `uvicorn orchestrator.api.main:create_app --factory --host 127.0.0.1 --port 8765`.
  See [ADR-0012](docs/ADR%20documentation/0012-fastapi-skeleton-architecture.md).
- **`GET /api/v1/health`** endpoint per Bible §8.4. Returns the 7-field
  response (status / version / uptime_sec / scheduler_running /
  lancache_reachable / cache_volume_mounted / validator_healthy /
  git_sha) with HTTP 200 if all subsystems healthy, 503 otherwise.
  **Note:** BL5 ship state intentionally returns 503 because three
  subsystems (`scheduler_running`, `lancache_reachable`,
  `validator_healthy`) are stub-false until BL6+ flips them as features
  land. Container HEALTHCHECK and k8s liveness probes should expect 503
  during this transition.
- **OpenAPI schema** at `/api/v1/openapi.json`, **Swagger UI** at
  `/api/v1/docs`, **ReDoc** at `/api/v1/redoc`. `bearerAuth`
  security_scheme registered so Swagger UI's Authorize button works.
- **`asgi-lifespan==2.1.0`** added to `requirements-dev.txt` for
  test-time lifespan integration.

### Security
- **TM-013 fingerprinting defense:** bearer-auth implemented as pure
  ASGI middleware (not FastAPI Depends), so 404s on non-exempt paths
  also require auth. Returns 401 with timing-safe `hmac.compare_digest`
  comparison (UTF-8-encoded bytes, length-tolerant).
- **OQ2 loopback enforcement:** path pattern
  `^/api/v1/platforms/[^/]+/auth$` additionally requires
  `request.client.host == "127.0.0.1"`. The route is reserved in BL5
  (the actual handler lands in F1/F2); the middleware logic is in
  place so BL6+ inherits enforcement automatically.
- **TM-012 log redaction:** rejected bearer tokens logged with
  `rejection_fingerprint` (8 hex of SHA-256, non-reversible). Field
  name avoids the "token"/"auth"/"bearer"/"secret" keywords because
  ID3's `_redact_sensitive_values` would auto-redact them. Verified by
  `test_no_raw_token_in_logs` and `test_auth_rejected_event_emits_with_sha256_prefix`.
- **TM-018 memory bomb defense:** ASGI middleware enforces 32 KiB
  request-body cap (Bible §9.2). Two paths: Content-Length proactive
  check (immediate 413 before any read); streaming check via
  `receive()` interception (interrupts mid-stream when accumulated
  bytes exceed cap). Streaming variant verified by direct middleware
  unit test against a fake downstream app.
- **CORS hardened:** `allow_credentials=False`. Bearer-token auth flows
  in `Authorization` header, not cookies — closes the
  `allow_origins=*` + `allow_credentials=true` footgun by constraint.
  `allow_headers` whitelist: `Authorization`, `Content-Type`,
  `X-Correlation-ID`. `expose_headers`: `X-Correlation-ID` (for
  Game_shelf to log + correlate API calls).
- **Correlation-ID propagation:** outermost middleware enters ID3's
  `request_context()` per-request. Echoed in response header. Every
  log line during request processing carries the correlation_id via
  structlog contextvar — downstream debugging can grep one CID for
  the full request trace.

#### UAT-3 remediation (2026-04-30)

Empirical UAT pass plus 5 parallel agents surfaced 11 SEV-2 + 4 SEV-3
items against the BL5 surface; all live + queued items fixed test-first
in this revision. See
`tests/uat/sessions/2026-04-27-session-3/agent-results/_consolidated.md`
and `tests/api/test_uat3_remediation.py` (28 new regression tests).

- **S2-A — exempt-prefix exact-match.** `BearerAuthMiddleware` now uses
  exact-or-subpath matching keyed on a per-path `allow_subpaths` flag
  (`AUTH_EXEMPT_PATHS` in `dependencies.py`) instead of unanchored
  `startswith`. Closes the latent foot-gun where a future route like
  `/api/v1/healthcheck` would silently bypass auth.
- **S2-B — `git_sha` recon defense.** `/api/v1/health` truncates the
  `git_sha` field to 8 chars before returning it. Operators with CI
  pipelines that set `GIT_SHA` to the full 40-char commit hash no
  longer leak it pre-auth.
- **S2-C + S3-h — schema/UI loopback restriction.** `/api/v1/openapi.json`,
  `/api/v1/docs` (+ `/oauth2-redirect`), and `/api/v1/redoc` are now
  gated behind the OQ2 loopback check. Loopback access works (developers
  can browse Swagger), LAN access returns 403. IPv6 forms (`::1`,
  `::ffff:127.0.0.1`) are honored alongside IPv4 `127.0.0.1`.
- **S2-D — non-loopback bind warning.** Lifespan emits
  `api.boot.non_loopback_bind_warning` at WARNING when `api_host !=
  "127.0.0.1"`, with explicit hint about reverse-proxy OQ2 bypass risk.
  Phase 3 backlog item: optional `OQ2_TRUSTED_PROXIES` allowlist.
- **S2-F — CORS outermost.** Middleware order revised: CORS now wraps
  CorrelationId/BodySizeCap/BearerAuth so 401/413 short-circuit
  responses include `Access-Control-Allow-Origin` headers. Operators
  see real status codes in the browser instead of the misleading
  "CORS error" mask. Trade: CORS-rejected preflights lack a
  correlation_id (those rejections are rare and client-misconfigured).
- **S2-G — no duplicate `http.response.start`.** `BodySizeCapMiddleware`
  tracks a `response_started` flag in the wrapped send; if the cap
  trips after the downstream handler has begun streaming a response,
  the middleware logs and lets the connection close naturally instead
  of emitting a protocol-violating second start frame.
- **S2-I — module-level `app`.** `orchestrator.api.main` exposes a
  lazy `app` attribute via PEP 562 `__getattr__`. Standard
  `uvicorn orchestrator.api.main:app` and Dockerfile `CMD ["uvicorn",
  "orchestrator.api.main:app", ...]` patterns now work without the
  `--factory` flag. Lazy construction means just importing the module
  (e.g. for `create_app` in tests) doesn't load settings.
- **S3-m — RFC 7235 case-insensitive Bearer scheme.** Middleware
  accepts `bearer`, `BEARER`, `BeArEr`, etc. — HTTP scheme is
  case-insensitive per RFC 7235 §2.1.

### Changed
- **Middleware ordering revised** (UAT-3 S2-F). New outermost-→innermost
  order: `CORS → CorrelationId → BodySizeCap → BearerAuth`. Spec §5.1
  language updated; ADR-0012 D5 superseded by ADR-0012 addendum.
- **`AUTH_EXEMPT_PREFIXES` → `AUTH_EXEMPT_PATHS`.** Now a tuple of
  `(path, allow_subpaths)` pairs. Backwards-compatibility shim
  `AUTH_EXEMPT_PREFIXES = tuple(p for p, _ in AUTH_EXEMPT_PATHS)` kept
  for any external import.

### Fixed
- **S2-J — migration runner wraps `sqlite3.OperationalError`** as
  `MigrationError` so the lifespan's catch-and-`SystemExit(1)` contract
  holds for the most common operator failures (bad path, permission
  denied, read-only filesystem). Without this, raw sqlite3 errors
  produced a 50-line traceback instead of the documented structured
  `api.boot.migrations_failed` event.
- **S3-a — lifespan partial-init cleanup.** Post-init steps run inside
  `try/finally`; if any step after `init_pool()` raises, `close_pool()`
  still executes so writer/reader connections aren't leaked at process
  death.
- **S3-k — ASGI-headers redaction.** `_redact_sensitive_values` now
  detects the list-of-(bytes,bytes)-tuples shape used by `scope["headers"]`
  and applies the sensitive-key regex per pair. Eliminates the latent
  bypass if any future code logs `scope=scope`.

- **Async DB pool** (`src/orchestrator/db/pool.py`, BL4 / Feature 4) —
  hybrid 1-writer-N-reader topology on top of `aiosqlite`. Defense-in-depth
  write serialization (`asyncio.Lock` + `BEGIN IMMEDIATE` + `busy_timeout`).
  Comprehensive API: `read_one`/`read_all`/`read_one_as`/`read_all_as`/
  `read_stream`/`execute_write`/`execute_many_write` single-statement
  helpers, `read_transaction`/`write_transaction` multi-statement contexts,
  `acquire_reader`/`acquire_writer` raw-connection escape hatches. Module-
  level singleton (`init_pool`/`get_pool`/`reload_pool`/`close_pool`).
  See [ADR-0011](docs/ADR%20documentation/0011-db-pool-architecture.md).
- **`migrate.verify_schema_current()`** — async helper that asserts the
  applied migration set matches the packaged manifest. Called by
  `Pool.create()` unless `skip_schema_verify=True` (which logs
  `pool.schema_verification_skipped` at WARNING).
- **`pool.schema_status()`** — read-only introspection surface for
  `/api/v1/health` consumers; returns `{applied, available, pending,
  unknown, current}`.
- **`pool.health_check()`** — concurrent per-connection probe with 1 s
  per-probe timeout. Reports writer + reader health, replacement counts,
  uptime.
- **5 new typed Settings fields** (`pool_readers`, `pool_busy_timeout_ms`,
  `db_cache_size_kib`, `db_mmap_size_bytes`, `db_journal_size_limit_bytes`)
  driving pool sizing and SQLite PRAGMA tunables. See ADR-0010 addendum.
- **`config.pool_readers_over_provisioned` diagnostic warning** — fires
  when `pool_readers > chunk_concurrency` (readers will idle).

### Security
- **No raw SQL or parameter values reach log output** (TM-012). Every
  `pool.*` log emission uses `_template_only(sql)` (literals replaced
  with `?`) and `_shape(params)` (parameter type names only, never values).
  Hypothesis property tests in `tests/db/test_pool_property.py` exercise
  the scrubbers across arbitrary value shapes; capsys-based regression
  tests verify end-to-end log scrubbing through the structlog JSONRenderer.
- **Reader connections are read-only at the SQLite layer.** `PRAGMA
  query_only=ON` applied after open; writes through a reader handle fail
  with `OperationalError("readonly database")`. Defense-in-depth alongside
  the application-level reader/writer split.
- **PRAGMA verification at boot.** Each of 9 PRAGMAs (busy_timeout,
  foreign_keys, synchronous, temp_store, cache_size, mmap_size,
  journal_size_limit, plus reader-only query_only) is set then read back;
  mismatch raises `PoolInitError(role=...)` and aborts pool startup.
  Defends against silent SQLite ABI changes that could drop a PRAGMA.
- **Connection-replacement storm guard.** Per-role 60-second sliding
  window; >3 replacements trips the guard and refuses further auto-recovery
  (pool transitions to degraded; operator must `reload_pool()`).
  Prevents disk-failure storms from amplifying into infinite-reconnect
  CPU/IO loops.
- **Background-task error logging** (SEV-3 finding from Phase 2.4 audit,
  fixed inline). Replacement and safe-close tasks now register a done
  callback that logs `pool.background_task_failed` at ERROR with task
  name, error message, and error type. Without this, replacement
  failures would have been silently swallowed by asyncio defaults.
- Correlation-ID leak fix: `request_context()` now uses structlog's
  token-based reset, so nested context managers restore the outer block's
  CID rather than wiping all contextvars. Eliminates cross-request bleed
  via pooled workers that was the core risk behind issue [#9](https://github.com/kraulerson/lancache-orchestrator/issues/9).
- User kwargs that collide with framework-owned reserved keys
  (`correlation_id`, `level`, `timestamp`, `event`, `logger`, `logger_name`)
  are now rescued to `user_<key>` (with numbered-slot collision handling)
  rather than silently overriding. Protects audit-trail integrity against
  attacker-controlled input reaching `log.info(**user_dict)`. (Issue [#10](https://github.com/kraulerson/lancache-orchestrator/issues/10))
- Recursive secret-value redaction: any log-event key matching the
  sensitive-key regex (password, passwd, passphrase, token, jwt, secret,
  authorization, bearer, cookie, session, api_key, apikey, credential,
  private_key, privkey, signature, plus letter-bounded pwd/pin/otp/mfa/
  tfa/sid/creds/salt/nonce) has its value replaced with `<redacted>`
  before the JSONRenderer sees it. Walks nested dicts and lists.
  Cycle-safe — a self-referential structure is substituted with
  `<cyclic>` rather than blowing the stack. (Issue [#14](https://github.com/kraulerson/lancache-orchestrator/issues/14),
  re-audit N3+N4)
- Migrations runner now refuses to boot on network filesystems (NFS, CIFS,
  SMB, GlusterFS, Ceph, Lustre, BeeGFS, GPFS, OCFS2, GFS2, MooseFS, plus
  FUSE-backed `sshfs`/`cifs`/`smb`/`glusterfs`/`s3fs`/`gcsfuse`/`goofys`).
  Opt-in `ORCH_REQUIRE_LOCAL_FS=strict` upgrades unknown-fs to hard failure
  for deployments where silent WAL corruption is worse than refusing to
  start. (Issues [#12](https://github.com/kraulerson/lancache-orchestrator/issues/12), re-audit F1+F2)
- Pinned SHA-256 checksums for every packaged migration in a new
  `CHECKSUMS` manifest. Tamper of an unapplied migration is now detected
  before apply. Supply-chain defense: an attacker modifying a migration
  file must also modify the manifest in the same commit. (Issue [#5](https://github.com/kraulerson/lancache-orchestrator/issues/5))
- Post-apply schema-object sanity check derived from each migration's SQL
  now runs inside the transaction before COMMIT, so a failure triggers
  ROLLBACK. Prevents the boot-loop failure mode where `schema_migrations`
  claims migrations are applied but the expected tables are missing. (Issue [#6](https://github.com/kraulerson/lancache-orchestrator/issues/6), re-audit F6)

### Data Model
- `0001_initial.sql` relocated to the `orchestrator.db.migrations` Python
  subpackage. Runner now loads migrations via `importlib.resources.files()`
  rather than a `__file__`-relative filesystem path — mitigates the
  "attacker-writable app dir → arbitrary DDL on restart" class of risk.
  (Issue [#13](https://github.com/kraulerson/lancache-orchestrator/issues/13))
- Header comment in `0001_initial.sql` corrected — previous version
  falsely claimed atomicity that the implementation didn't deliver.

### Added
- `MigrationError` typed exception for all migrations-framework failures.
- `tests/db/test_migrate.py` (42 tests) covering every UAT-1 finding and
  every re-audit hardening item.
- `docs/security-audits/id1-sqlite-migrations-security-audit.md` records
  the full pre- and post-fix audit trail.
- ADR-0008 documents the atomicity / checksum / packaging decisions.
- `orchestrator.core.logging.request_context()` context manager for
  scoped correlation-ID binding. Supersedes the raw `bind_correlation_id()`
  + `clear_request_context()` pair, which remain as low-level primitives.
- Public `RESERVED_KEYS` constant exported from `orchestrator.core.logging`.
- `tests/core/test_logging.py` (55 tests) covering every UAT-1 + re-audit
  logging finding.
- `docs/security-audits/id3-structured-logging-security-audit.md` records
  the logging audit trail.
- ADR-0009 documents the scoped-context / reserved-key / redaction /
  log-level-validation decisions.
- `src/orchestrator/core/settings.py` (ID4) — typed application configuration
  via pydantic-settings `BaseSettings`. 16 fields covering API (`api_host`,
  `api_port`, `cors_origins`, `log_level`, `orchestrator_token`), database
  (`database_path`, `require_local_fs`), platform sessions
  (`steam_session_path`, `epic_session_path`), Lancache cache topology
  (`lancache_nginx_cache_path`, `cache_slice_size_bytes`, `cache_levels`,
  `chunk_concurrency`), and miscellaneous (`manifest_size_cap_bytes`,
  `epic_refresh_buffer_sec`, `steam_upstream_silent_days`). Defaults
  sourced from Bible §7.2/§7.3/§9, Spike F, and the Lancache deployment
  params memory.
- `orchestrator.core.settings.get_settings()` — `@lru_cache` singleton
  accessor. `reload_settings()` provided as a test / SIGHUP escape hatch.
- Four diagnostic `@model_validator(mode="after")` warnings:
  `config.secret_shadowed_by_env` (env and `/run/secrets` both set),
  `config.api_bound_non_loopback` (`api_host` isn't loopback),
  `config.cors_wildcard` (`"*"` in `cors_origins`),
  `config.chunk_concurrency_unvalidated` (`chunk_concurrency > 32`, the
  Spike F gate ceiling).
- `tests/core/conftest.py` — shared autouse `_isolated_env` fixture that
  scrubs `ORCH_*` env vars, chdirs to `tmp_path` (blocks host `.env`
  discovery), resets structlog defaults + contextvars (matching the ID3
  test pattern), and clears the `get_settings()` cache before and after
  every test in `tests/core/`.
- `tests/core/test_settings.py` (67 tests) — full coverage of required
  fields, the 15 optional defaults, field validators, source precedence,
  secret-loading paths, 5-shape × 3-serialization redaction parametrize,
  4 warnings + 1 negative case, singleton behavior, and 2 SEV-2
  regression tests (pickle-block, ValidationError scrubbing).
- `docs/security-audits/id4-settings-security-audit.md` records the
  audit trail.
- ADR-0010 documents the flat-layout / source-order / singleton /
  redaction-layer / validation-scope decisions.

### Changed
- `run_migrations()` rewritten: explicit `BEGIN IMMEDIATE` wraps the whole
  read+apply pass; PRAGMAs run outside any transaction; per-statement
  `conn.execute()` inside the transaction (instead of `executescript()`,
  which auto-commits and defeated atomicity). (Issue [#3](https://github.com/kraulerson/lancache-orchestrator/issues/3))
- Gap migrations are now rejected with a hard error naming the missing ID,
  instead of being silently skipped. (Issue [#4](https://github.com/kraulerson/lancache-orchestrator/issues/4))
- Concurrent runners serialize cleanly via `PRAGMA busy_timeout = 5000`
  combined with the single `BEGIN IMMEDIATE`; the losing runner no-ops
  after re-reading `applied_map`. (Issue [#8](https://github.com/kraulerson/lancache-orchestrator/issues/8))
- `configure_logging(log_level=...)` now validates input against
  `{DEBUG, INFO, WARNING, ERROR, CRITICAL}` (case-insensitive, stripped).
  Raises `ValueError` on anything else instead of silently falling back
  to INFO — operator typos in `LOG_LEVEL` surface at startup rather than
  at incident time. (Issue [#15](https://github.com/kraulerson/lancache-orchestrator/issues/15))

### Fixed
- Migration atomicity — see Security/Changed entries above. (Issue [#3](https://github.com/kraulerson/lancache-orchestrator/issues/3))
- Silent-skip of gap / out-of-order migrations. (Issue [#4](https://github.com/kraulerson/lancache-orchestrator/issues/4))
- Drift detection on unapplied migrations. (Issue [#5](https://github.com/kraulerson/lancache-orchestrator/issues/5))
- `schema_migrations` tamper bypass. (Issue [#6](https://github.com/kraulerson/lancache-orchestrator/issues/6))
- Concurrent-runner race. (Issue [#8](https://github.com/kraulerson/lancache-orchestrator/issues/8))
- WAL journal-mode unconditionally set without FS probe. (Issue [#12](https://github.com/kraulerson/lancache-orchestrator/issues/12))
- Correlation-ID context bleed across pooled workers. (Issue [#9](https://github.com/kraulerson/lancache-orchestrator/issues/9))
- Reserved-key clobber from user kwargs. (Issue [#10](https://github.com/kraulerson/lancache-orchestrator/issues/10))
- Missing PII/secret redaction in log values. (Issue [#14](https://github.com/kraulerson/lancache-orchestrator/issues/14))
- `log_level` silent fallback to INFO on typo. (Issue [#15](https://github.com/kraulerson/lancache-orchestrator/issues/15))
- Short-token redaction regex silently failed on `user_pwd` / `my_pin` /
  `otp_code` / `creds_list` etc. shapes because Python `\b` uses `\w`
  boundaries and `_` is `\w`. Replaced with letter-class boundaries.
  **Caught and fixed before ship** by the BL2 re-audit pass. (Re-audit N3)
- Settings module redaction primitives: `SecretStr` is supplemented by
  a `__reduce__` override that blocks pickling (pydantic's default
  pickler serialises `_secret_value` cleartext, which any future DX
  sugar like multiprocessing task args or Celery would write to an
  attacker-readable queue). `Settings.__init__` intercepts pydantic's
  `ValidationError` for token-field failures and re-raises as
  `ValueError` with a scrubbed message — pydantic core otherwise
  echoes the raw rejected token in `input_value`, which a rotation-
  failure startup would land in the systemd journal. **Caught and
  fixed before ship** by the BL3 re-audit pass. (Audit A1 + A2)

### Removed
- `migrations/0001_initial_down.sql` and all doc references to
  `orchestrator-cli db rollback`. Rollback is intentionally out of MVP
  scope; re-introducing it will require a dedicated ADR covering
  versioning and data-preservation policy. (Issue [#7](https://github.com/kraulerson/lancache-orchestrator/issues/7))
- Top-level `migrations/` directory (contents moved into the package).

### Infrastructure
- `pyproject.toml`: added `[tool.setuptools.package-data]` to ship
  `*.sql` + `CHECKSUMS` inside the `orchestrator.db.migrations` package.
  Per-file ruff `S101/S105/S106` ignore for `tests/core/test_logging.py`
  (redaction tests necessarily include fake credential literals as inputs).
- `Dockerfile`: removed the `COPY migrations/ /app/migrations/` step
  (migrations now ride along inside the installed wheel).
- `.semgrep/orchestrator-rules.yaml`: `no-sync-sqlite` rule now excludes
  `tests/db/test_migrate.py`. `no-credential-log` rule now excludes
  `tests/core/test_logging.py` — redaction tests verify the processor by
  logging literal credential-named kwargs and asserting the value becomes
  `<redacted>`.

### Documentation
- New ADR: [`ADR-0008 — Migration Runner Architecture`](docs/ADR%20documentation/0008-migration-runner-architecture.md).
- New ADR: [`ADR-0009 — Logging Framework Architecture`](docs/ADR%20documentation/0009-logging-framework-architecture.md).
- New audit artifacts:
  `docs/security-audits/id1-sqlite-migrations-security-audit.md`,
  `docs/security-audits/id3-structured-logging-security-audit.md`, and
  `docs/security-audits/id4-settings-security-audit.md`.
- FEATURES.md now documents Feature 1 (ID1 migrations), Feature 2
  (ID3 structured logging), and Feature 3 (ID4 settings module) with
  links, known limitations, and test-coverage summaries.
- New ADR: [`ADR-0010 — Settings Module Design`](docs/ADR%20documentation/0010-settings-module-design.md).
- Design spec at `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`
  and implementation plan at `docs/superpowers/plans/2026-04-23-id4-settings-module.md`
  record the 14-decision brainstorm and 11-task execution trail for BL3.
