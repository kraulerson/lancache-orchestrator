# Security Audit — Steam manifest-only fetcher (DepotDownloader)

**Feature:** steam-manifest-fetcher-dd (DepotDownloader `-manifest-only` → `.shas` sidecars → closes the validation-coverage gap)
**Modules:**
- `src/orchestrator/platform/steam/manifest_fetcher.py` — `DepotDownloaderManifestFetcher`: enumerate, fetch per-app, write `.shas`
- `src/orchestrator/platform/steam/steamkit_manifest_parser.py` — parses DepotDownloader's SteamKit2 `.manifest` protobuf output → chunk SHA1 set
- `src/orchestrator/agent/routers/steam.py` — `POST /v1/steam/fetch-manifests` agent endpoint (single-flight guard)
- `src/orchestrator/api/routers/fetch_manifests_trigger.py` — `POST /api/v1/fetch-manifests` (bearer-gated control-plane trigger)
- `src/orchestrator/scheduler/jobs.py` — `enqueue_fetch_manifests` callback
- `src/orchestrator/scheduler/manager.py` — weekly cron registration (`fetch_manifests_cron`, Monday 05:00 UTC)
- `src/orchestrator/cli/commands/cache.py` — `orchestrator-cli cache fetch-manifests` command
- `src/orchestrator/jobs/handlers/fetch_manifests.py` — control-plane job handler: delegates to `agent_client.fetch_manifests()`, records result
- `Dockerfile` — builder stage downloads, sha256-verifies, and unpacks the DepotDownloader binary; runtime COPY only
**Audit date:** 2026-06-30
**Auditor:** self-review (Senior Security Engineer persona) + ruff (flake8-bandit `S`) + mypy --strict + full suite
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-06-30 -->

## Scope

The manifest-only fetcher closes the validation-coverage gap: SteamPrefill only writes a manifest `.bin` when an app has new content (and prunes manifests via `clear-temp`), so its cache covered only ~330 of ~1077 prefilled apps. The fetcher calls DepotDownloader with `-manifest-only` per app, writing a `.shas` sidecar (one chunk SHA per line) into the manifest archive. The validator already reads `.shas` files alongside `.bin` files, so once the fetcher runs, validation coverage reaches the full prefilled set without re-downloading any game content. The agent self-enumerates its cached set from the local DB, making the job weekly-schedulable and unattended after a one-time 2FA login.

Packaging: DepotDownloader is a self-contained .NET 8 linux-x64 binary, pinned by version (`3.4.0`) and sha256 (`a999dec6…`) in the Dockerfile builder stage. The runtime image carries the binary at `/depotdownloader/DepotDownloader`; no `.NET` runtime is needed in the image (the binary bundles it). The image is shared between the control plane and the agent, but only the agent invokes the binary.

## Methodology

1. **SAST-lite.** `ruff check src/orchestrator tests` (flake8-bandit `S`) — clean.
2. **Type safety.** `mypy src/orchestrator` — clean (all modules).
3. **Import isolation.** The agent must not import `orchestrator.api.main`/`db.pool`; the fetcher is agent-internal and stdlib-only for I/O — `tests/agent/test_import_isolation.py` still green.
4. **Threat-model cross-check:** argv injection, secrets, path traversal, availability, supply-chain / binary pinning.
5. **Tests.** Full suite passed (only the documented `tests/test_licenses.py` local pip-licenses failure).

## Audit findings

| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (explicitly checked, clean)

- **No argv injection.** `app_id` and `depot_id` values passed to DepotDownloader come from the local DB (`games.app_id`, manifest archive filenames) and are validated as non-negative integers before use. The subprocess is launched via `subprocess.run([...], ...)` with a list — no shell interpolation, no `shell=True`. A non-integer app_id is rejected upstream by the endpoint's Pydantic model (`int`) and the DB schema (`INTEGER`). There is no user-controlled string that reaches the argv list.
- **Secret handling.** DepotDownloader persists only its own token/login-key under the operator-supplied `config_dir` volume. Our code passes `-username` (a non-secret identifier) and `-remember-password` to enable token reuse; the Steam password and 2FA code are typed interactively by the operator in a one-time `docker exec` session and are never written to any log line, environment variable, or DB column by our code. The `-password` flag is not used programmatically — the interactive login path is the only credential path. Subsequent unattended runs reuse the persisted login key.
- **No path traversal.** Archive writes produce files named `{app_id}_{app_id}_{depot_id}_{manifest_gid}.shas` where all four fields are integers extracted from the DepotDownloader output (parsed as `int`). The output directory is a fixed, settings-derived path (`steam_manifest_archive_dir`). No caller-controlled string component reaches the output path; `pathlib.Path` join with integer-stringified fields cannot traverse outside the archive root.
- **Availability isolation.** Each app's fetch runs in its own `try/except Exception` block; a DepotDownloader crash or non-zero exit for one app is logged and counted as `failed` without aborting the rest of the batch. A hard `except BaseException` boundary at the job level (inherited from the dispatcher) ensures even a signal or `KeyboardInterrupt` during the batch records a terminal job state instead of leaving the job hanging. The capture of `.shas` output never fails the job — a write error is logged and the app is counted `failed` for that run; the archive is append-only and the next weekly run retries it.
- **Supply-chain / binary pinning.** The DepotDownloader zip is fetched from the official GitHub release URL (`github.com/SteamRE/DepotDownloader`) over TLS with `curl -fsSL` and immediately verified with `sha256sum -c -` before extraction. The `DEPOTDOWNLOADER_SHA256` ARG is hardcoded in the Dockerfile (`a999dec6…`); a tampered or substituted zip will fail the sha256 check and abort the build. The pinned version is `3.4.0`, a stable release tag (not a floating `latest`). The binary never runs during image build — only during agent runtime when a `fetch_manifests` job is dispatched.
- **No new network surface.** The control plane has no direct DepotDownloader interaction. The agent already reaches out to Steam CDN for prefill/validate; the manifest-only fetcher adds no new outbound destination category. The trigger endpoint (`POST /api/v1/fetch-manifests`) is bearer-gated, consistent with all other write endpoints.
- **pip-audit / license unaffected.** DepotDownloader is a pre-built binary, not a Python package; it does not appear in `requirements.txt` and is not scanned by `pip-audit` or `pip-licenses`. The license scan (`tests/test_licenses.py`) and `pip-audit` CI step are unaffected.

## Decision

**Cleared to advance.** No SEV-1/2/3/4 findings. Additive, secret-handling-correct, injection-free, path-traversal-free, crash-safe (per-app isolation + BaseException boundary), supply-chain-pinned (sha256 at build time), no new network surface, pip-audit/license unaffected.

## Sign-off

- Implementation: commits on `feat/steam-manifest-fetcher-dd`
- Test suite: 1350 passed (`--ignore=tests/scripts`); only pre-existing `tests/test_licenses.py` fails
- ruff + mypy clean; agent import-isolation preserved
- No new Python dependency; no migration
- Binary: DepotDownloader 3.4.0 sha256 `a999dec66b4850fc961bd50366696d23c2d0fad7b18790e6a5647b2f19097a53` (linux-x64, self-contained .NET 8)
