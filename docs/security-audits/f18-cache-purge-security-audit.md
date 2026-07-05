# Security Audit — F18 Operator-Driven Cache Purge

**Feature:** f18-cache-purge — a reversible, operator-driven per-game cache purge. The control
plane enqueues a `purge` job; the data-plane agent enumerates the game's chunk paths (exactly as
validate does) and `unlink`s the present ones; the control handler then sets
`status='validation_failed'` so F5/F6 re-prefills a clean copy.
**Modules:** `validator/disk_stat.py` (`purge_chunks`), `agent/_paths.py` (`under_cache_root`),
`agent/routers/steam.py` + `agent/routers/epic.py` (purge endpoints + shared enumeration),
`clients/agent_client.py` (`steam_purge`/`epic_purge`), `jobs/handlers/purge.py`,
`api/routers/purge_trigger.py`, `cli/commands/game.py`, migration `0014_jobs_kind_purge.sql`.
**Audit date:** 2026-07-05 · **Auditor:** Senior Security Engineer persona + ruff (S) + mypy + full suite (1557) · **Phase:** 2, Build Loop 2.4

<!-- Last Updated: 2026-07-05 -->

## Methodology
The threat that matters for a *delete* feature is **deleting the wrong files** — path traversal,
symlink escape, an over-broad target set, or an unauthenticated caller. Each is traced from input
to the `unlink()` syscall. ruff (S — bandit rules) clean, mypy clean, full suite green (1557; only
the pre-existing `test_licenses` tooling gap fails). Reversibility (ADR-0015) is the backstop: every
deleted chunk re-downloads from the CDN on the next prefill, so the worst realistic outcome of a
bug is transient WAN re-download, not data loss.

## Threat model — "I can reach the purge endpoint, what can I delete?"

1. **Cross the cache boundary (traversal / symlink escape).** The delete target set is built by the
   validate enumeration: manifest chunk SHAs → `steam_chunk_uri`/`epic_chunk_uri` → `cache_key`
   (md5 hex) → `cache_path`. `cache_path` rejects any hash that is not 32 lowercase hex
   (`_HEX32_RE`) and asserts the computed path `is_relative_to(cache_root)`. On top of that, every
   purge routes its path list through `under_cache_root(cache_root, paths)`, which drops any path
   whose `Path.resolve()` (collapsing `..` and following symlinks) is not strictly inside the
   resolved cache root — and drops the root directory itself. A crafted manifest, a symlink planted
   in the cache tree, or a future enumeration bug therefore cannot escape the cache directory.
   Direct unit tests: `tests/agent/test_paths.py` (dotdot traversal dropped, symlink-escape
   dropped, root-itself dropped, mixed list keeps only inside paths).

2. **Delete more than one game.** The enumeration is scoped to a single `app_id`: Steam locates
   only that app's manifest `.bin`/`.shas` files; Epic parses only the one manifest the control
   plane sent. There is no cache-wide, depot-wide, or chunk-arbitrary delete path — the API accepts
   only a `game_id` and the agent accepts only an `app_id` (Steam) or one manifest (Epic).

3. **Delete without authorization.** `POST /api/v1/games/{game_id}/purge` is behind the API bearer
   auth (401 without/with a wrong token — `tests/api/test_purge_trigger_router.py::TestAuthBoundary`).
   The agent purge endpoints sit behind the agent's global `BearerAuthMiddleware`
   (`tests/agent/test_steam_purge.py::test_steam_purge_requires_auth`,
   `test_epic_purge_no_bearer_returns_401`) — auth runs before routing, so even the endpoint path is
   not reachable unauthenticated.

4. **Inject via the job/DB path.** All SQL is parameter-bound (`?` placeholders) — the migration,
   the trigger INSERT/SELECT, and the handler status UPDATE. `game_id` is a typed path int; the
   `purge` kind is a static literal. No string interpolation reaches SQL.

## Findings
| # | Severity | Title | Status |
|---|----------|-------|--------|
| — | — | No findings. | — |

## Non-findings (checked, clean)
- **`purge_chunks` never raises + best-effort.** A missing path is an idempotent no-op; an
  `OSError` on a present file counts as `failed` and is swallowed (re-prefill is the safety net).
  Bytes are counted only after a successful `unlink`, so a present-but-undeletable file frees 0.
- **Idempotency = no destructive surprise on retry.** A never-cached game returns `{deleted:0}`
  (Steam: no manifest / no files; Epic: nothing on disk). The in-flight UNIQUE index
  (`idx_jobs_purge_inflight`) + `ON CONFLICT DO NOTHING` collapse concurrent triggers onto one job.
- **AgentError does not falsely flag a game.** If the agent-side delete fails, the exception
  propagates and the handler never reaches the `status='validation_failed'` write — the game's
  status is left as-is (`tests/jobs/test_purge_handler.py::test_agent_error_leaves_status_unchanged`).
- **Epic "no manifest" is a clear error, not a silent no-op** (ADR-0015): the handler raises rather
  than sending an empty delete, so an un-manifest-fetched game surfaces the gap instead of a
  misleading success.
- **Reversibility invariant preserved.** Purge sets `validation_failed` and never touches
  `block_list` / `prefill_exclusions`. A block-listed game therefore purges but does not
  re-download (its scheduled prefill stays suppressed) — "block + purge" reclaims space
  permanently, with no separate mode.

## The conscious trade-off (from ADR-0015)
The agent's lancache cache mount is relaxed `:ro → :rw` so the agent can `unlink`. This removes a
defense-in-depth property — a buggy or compromised agent can now damage the cache. It is bounded
by: the two-layer path guard (`_HEX32_RE` in `cache_path` + `under_cache_root`), the single-game
scope, bearer auth on both hops, and reversibility (deleted chunks re-download). ADR-0015 records
this as an accepted, deliberate reduction, not an oversight. The alternative (a separate
write-scoped purge sidecar) was considered and deferred as heavier than relaxing one mount flag.

## Decision
No findings. Ship. Deploy note: the agent redeploy (`deploy-agent.sh`) must change the cache mount
`:ro → :rw`; verify post-deploy with `docker inspect orchestrator-agent` (`RW=true` for
`/data/cache`), then a live purge of a small test game → validate shows the chunks missing →
prefill heals → validate green.
