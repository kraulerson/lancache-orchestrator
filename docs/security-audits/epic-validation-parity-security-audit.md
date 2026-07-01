# Security Audit — Epic Cache-Validation Parity

**Feature:** Epic validation parity — per-chunk, on-disk disk-stat validator for Epic games
**Modules:**
- `src/orchestrator/agent/routers/epic.py` — `POST /v1/epic/validate` agent endpoint
- `src/orchestrator/validator/disk_stat.py` — `validate_chunks_any`, `_stat_any_batch`
- `src/orchestrator/validator/cache_key.py` — `epic_chunk_uri`, `cache_key`, `cache_path`
- `src/orchestrator/platform/epic/manifest.py` — `parse_manifest`, `fetch_manifest`
- `src/orchestrator/jobs/handlers/validate.py` — platform dispatch, Epic branch
- `src/orchestrator/jobs/handlers/sweep.py` — dropped `platform='steam'` restriction
- `src/orchestrator/clients/agent_client.py` — `epic_validate`
- `src/orchestrator/core/settings.py` — `epic_cache_identifiers`
- `src/orchestrator/db/migrations/0010_manifests_cdn_base.sql` — `manifests.cdn_base`

**Audit date:** 2026-07-01
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy --strict + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-07-01 -->

## Scope

Closes the Epic validation gap: Epic games previously had no disk-stat validator; the only check was `verify_cached` (a sample-based network re-request counting lancache HITs at prefill time). This feature introduces a real per-chunk on-disk validator at parity with the Steam F7 validator: for each Epic chunk, it derives the lancache cache-key (`md5(identifier + cdn_base/chunk_path + "bytes=0-10485759")`) across a configurable identifier set and stats the computed path under `/data/cache`, counting a chunk present if cached under **any** identifier. The control plane reads the stored manifest from the DB and hands the raw bytes + `cdn_base` to the agent; the agent does the parsing and disk work. A new `manifests.cdn_base` column (migration 0010) persists the CDN base path that was previously dropped after prefill.

Key security question: the manifest bytes and `cdn_base` passed to the agent originate from Epic's CDN response at prefill time. Can a hostile manifest, a corrupt DB row, or an adversarial agent caller cause path traversal, SSRF, information disclosure, or denial-of-service?

## Methodology

1. **SAST-lite.** `ruff check src/orchestrator tests` (flake8-bandit `S`) — clean.
2. **Type safety.** `mypy src/orchestrator` — clean (93 files; 0 issues).
3. **Import isolation.** The agent router must not pull in `orchestrator.api.main` / `orchestrator.db.pool`; `tests/agent/test_import_isolation.py` still green.
4. **Threat-model cross-check:** input provenance, injection, path traversal, SSRF, decompression bomb, DoS surface, secret exposure, availability.
5. **Tests.** Full suite: 1378 passed, 1 known failure (`tests/test_licenses.py` — pre-existing, no `pip-licenses` binary), 3 deselected (`--ignore=tests/scripts`).

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No new findings. | — |

## Non-findings (explicitly checked, clean)

### No network I/O, no auth at validate time

`agent/routers/epic.py::epic_validate` performs exactly: `base64.b64decode(body.raw_manifest_b64)` → `parse_manifest(...)` → `chunk_path` derivations → `cache_key`/`cache_path` computations → `validate_chunks_any(...)` (stat-only). The import list in the router is entirely stdlib + structlog + fastapi + `platform/epic/manifest.py` + `validator/cache_key.py` + `validator/disk_stat.py`. No `httpx`, no `asyncio.create_task`, no socket call, no credential read. The agent makes no outbound connection during validation.

### Input provenance: cdn_base and raw_manifest_b64 from the control DB, originally validated at fetch time

`cdn_base` and the manifest bytes the agent receives originate from `platform/epic/manifest.py::fetch_manifest`, which runs at Epic prefill time (not validate time) and imposes two hard guards before storing either value:

1. **Hostname validation (`_HOSTNAME_RE`):** `^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$` — enforces a plausible public FQDN (at least one dot; all-ASCII; no IP addresses, no `localhost`, no bare labels). An MITM'd or hostile manifest response that tries to route traffic to an internal target is rejected here. This check happens on the `cdn_host` from the signed URI **before** the manifest download request is made (adversarial-review fix from UAT-10).
2. **Path traversal guard (`".." in cdn_base`):** `cdn_base = parsed.path.rsplit("/", 1)[0]` (the directory portion of the signed path); if it contains `..` an `EpicManifestError` is raised immediately. Combined with `_HOSTNAME_RE`, the `cdn_base` stored in the DB is a clean, traversal-free path segment.

The manifest bytes are stored and later retrieved from a row the orchestrator controls (SQLite under `lancache_nginx_cache_path`). The only agent-facing surface is the RPC body, and `EpicValidateRequest` is a Pydantic model with `extra="forbid"`, `app_id: int = Field(..., ge=0)`, and `version: str`, `cdn_base: str`, `raw_manifest_b64: str` — all typed and validated before reaching any path logic.

### Decompression bomb + chunk-count DoS cap in parse_manifest

`parse_manifest` in `platform/epic/manifest.py` imposes three explicit DoS guards, all with documented rationale:

- **`_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024`:** `_decompress_capped` uses `zlib.decompressobj().decompress(data, max_length)` and checks `unconsumed_tail or not eof` — the capped decompressor never allocates beyond 256 MiB regardless of compressed input size. A tiny `zlib` stream that inflates to gigabytes is rejected before allocation.
- **`_MAX_CHUNKS = 5_000_000`:** `chunk_count` is checked immediately after reading; an implausible count raises `EpicManifestError` before any per-chunk allocation. This bounds the `candidate_lists` in the validator to at most 5 M entries.
- **`_MAX_PREREQ = 100_000`:** prereq-count loop guard preventing an unbounded prereq scan before the chunk list is reached.

Any parse failure (structural, bomb, count violation) raises `EpicManifestError`, which `epic_validate` catches and returns as `_err("manifest_parse_failed")` — never a 500, never a crash.

### Path traversal: cache-key paths are md5 hex + fixed level slices, structurally escape-proof

The cache-key computation chain is:

1. `chunk_path(chunk, manifest.version)` → `"ChunksV5/{group:02d}/{b64hash}_{b64guid}.chunk"` — all fields are either fixed-format base64-URL or uppercase hex derived from the manifest's binary GUID/hash fields. No attacker-controlled string component.
2. `epic_chunk_uri(cp, body.cdn_base)` → `f"{cdn_base_path.rstrip('/')}/{chunk_path}"` — `cdn_base` is the pre-validated, `..`-free path segment from the DB.
3. `cache_key(ident, uri, slice_range)` → `hashlib.md5(payload, usedforsecurity=False).hexdigest()` — the output is always 32 lowercase hex characters; the md5 input is never written to disk.
4. `cache_path(cache_root, h, levels)` — consumes fixed-width slices of the 32-char hex hash (`h[-2:]`, `h[-4:-2]`, `h` for `levels="2:2"`). The only caller-influenced component of the path is the identifier string (from `settings.epic_cache_identifiers`, a settings value not from the request); even so, `cache_path` contains an explicit backstop:

```python
if not result.is_relative_to(cache_root):
    raise ValueError(f"computed cache path {result} escapes root {cache_root}")
```

An identifier or `cdn_base` with `..` would have to survive an md5 hash before it could influence the directory components — structurally impossible. The traversal guard is nonetheless present as a correctness backstop.

### Per-chunk stat isolation: bounded executor, no event-loop blocking, symlink rejection

`validate_chunks_any` uses the existing dedicated `_get_cache_stat_executor()` — a `ThreadPoolExecutor(max_workers=2, thread_name_prefix="cache-stat")` — not `run_in_executor(None, ...)`. This ensures a hung NFS/FUSE mount stalling `stat()` threads can block at most the cache-stat pool, not the asyncio default pool (which handles DNS, probes, and HTTP). The worker count cap (2) bounds the number of threads that can be starved simultaneously. Each `_stat_any_batch` call skips symlinks (`p.is_symlink()` check before `p.stat()`), preventing symlink-following as a traversal primitive.

### Import isolation preserved

`tests/agent/test_import_isolation.py::test_agent_app_does_not_import_api_main_or_pool` runs the agent app in a subprocess and asserts `orchestrator.api.main` and `orchestrator.db.pool` are not in `sys.modules` after `import orchestrator.agent.app`. The Epic router imports only `platform/epic/manifest.py`, `validator/cache_key.py`, `validator/disk_stat.py`, plus stdlib and fastapi. The import-isolation test remains green with the new router included in `agent/app.py`.

### No new secret exposure

`epic_cache_identifiers` contains CDN hostnames/identifiers — not secrets. The `cdn_base` column stores a CDN path prefix (e.g. `/Builds/Org/{catalogId}/{buildId}/default`) — public metadata, not a credential or token. The agent RPC body contains `raw_manifest_b64` (the manifest bytes) and `cdn_base` — neither is a secret. No log field in `epic_validate` carries a token; the parse-failed warning truncates the error reason at 200 characters.

### Sweep un-scoping (dropping platform='steam') is additive-only

`jobs/handlers/sweep.py` previously emitted `WHERE status IN ('up_to_date','validation_failed') AND platform='steam'`. Removing `AND platform='steam'` means epic games with those statuses are now candidates. The per-game `validate_game` dispatch handles both platforms; a game with an unknown platform returns `ValidationResult(..., "error", ...)` — sweep per-game isolation means one error does not abort the batch.

### Pre-migration rows (cdn_base=NULL) handled safely

`_validate_epic_game` checks `if not manifest["cdn_base"]: return ValidationResult(0, 0, 0, "error", manifest["version"], "no_cdn_base")` before any agent call. The agent is never called with a NULL cdn_base. Status is left unchanged (mirrors the Steam `no_manifest_in_cache` behavior). No crash, no partial write.

## Decision

**Cleared to advance.** No SEV-1/2/3/4 findings introduced. The validator is network-free and credential-free at runtime; input provenance is sound (cdn_base and manifest bytes SSRF/traversal-validated at fetch time, stored in the orchestrator DB); parse_manifest has decompression-bomb + count caps; cache-key paths are structurally traversal-proof (md5 hex + fixed level slices) with a containment backstop; the bounded stat executor isolates disk I/O; import isolation is preserved; no new secrets are logged or transmitted.

**Residual risks (pre-existing, not introduced here):**
- Manifest bytes in the DB originate from Epic's CDN — if Epic's CDN were compromised and served a hostile manifest that satisfied the binary header check but was crafted to exhaust the chunk-count cap (5 M), the agent would iterate 5 M `stat()` calls. This is bounded (terminates; the pool is bounded; the job isolates the per-game error) and is a pre-existing property of the Epic prefill path.
- `cdn_base` is stored without further re-validation at validate time; the fetch-time `..` check and `_HOSTNAME_RE` are the sole guards. This is appropriate — the DB is the trusted source; the path was pre-validated before storage.

## Sign-off

- Implementation commits: 209b168, f2fa9e1, 97037ce, c96c8a4, ca701d7, c5422b9, 5350e85, edc2732
- Test suite: 1378 passed, 1 known failure (`tests/test_licenses.py`), 3 deselected (`--ignore=tests/scripts`)
- ruff format --check: 223 files already formatted (clean); ruff check: all checks passed
- mypy src/orchestrator: no issues found in 93 source files
- Agent import-isolation test: green
- No new dependency; migration 0010 (nullable ADD COLUMN, no table recreate)
