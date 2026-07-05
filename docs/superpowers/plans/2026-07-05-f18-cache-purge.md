# F18 Operator-Driven Cache Purge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reversible, operator-driven per-game cache purge — delete a game's chunk files, then let the existing validate/re-prefill path re-download a clean copy.

**Architecture:** Mirrors the validate flow (ADR-0015). The control plane enqueues a `purge` job; the **data-plane agent** (which alone holds the cache filesystem) enumerates the game's chunk paths exactly as validate does and `unlink`s the present ones; the control handler then sets `status='validation_failed'` so F5/F6 re-prefills next cycle and writes an audit `purge` job row. The agent's cache mount is relaxed `:ro → :rw`; deletes are bounded by 32-hex hash validation + a "resolved path must be under the cache root" guard.

**Tech Stack:** Python 3.12 / FastAPI (control on LXC 1105 + agent on .40), Click CLI, aiosqlite + STRICT SQLite migrations, React (Game_shelf, separate repo). Reuses `validator.cache_key.cache_path`, `agent.manifest_locator`, `agent.manifest_parser`, `platform.epic.manifest`.

## Global Constraints

- **Framework build-loop (per feature):** `scripts/process-checklist.sh --start-feature`, then complete in order `tests_written → tests_verified_failing → implemented → security_audit → documentation_updated → feature_recorded`. Commits are blocked until the loop is complete.
- **TDD mandatory:** write the failing test, watch it fail, implement, watch it pass. Never production code before a failing test.
- **Migrations:** numbered `NNNN_snake_case.sql` + a `CHECKSUMS` line (`<id>  <sha256>  <filename>`, sha via `shasum -a 256`). The `jobs.kind` CHECK is on a STRICT table → requires the rename-out → create-canonical → copy → drop table rebuild (mirror `0002_jobs_kind_manifest_fetch.sql` / `0009_jobs_fetch_manifests_unique.sql`). Statement order matters: the migrate runner's `_expected_tables_for` tracks CREATE/DROP TABLE but NOT ALTER RENAME.
- **Agent disk ops never raise to a 500:** endpoints return structured results; the control handler catches `AgentError`.
- **Reversibility invariant:** purge always sets `status='validation_failed'`; it never touches `block_list` or `prefill_exclusions`. A block-listed game therefore purges but does not re-download (the scheduled prefill skips `mode='exclude'` and block-listed rows).
- **Idempotent:** a never-cached game returns `{deleted: 0}`, HTTP 200 — never an error.
- **Path safety (security-critical):** the agent only unlinks paths whose 32-hex hash matches `^[0-9a-f]{32}$` AND whose `Path.resolve()` is a descendant of `settings.lancache_nginx_cache_path.resolve()`. Reject anything else.
- **Observability:** `game.purged` structured log `{game_id, platform, app_id, files_deleted, files_failed, total_bytes_freed}`.
- One PR per repo (orchestrator, then Game_shelf). Karl merges.

---

### Task 1: `purge_chunks` disk primitive (agent-side unlink)

**Files:**
- Modify: `src/orchestrator/validator/disk_stat.py` (add `purge_chunks` next to `validate_chunks` / `validate_chunks_any`)
- Test: `tests/validator/test_disk_stat.py`

**Interfaces:**
- Produces: `async def purge_chunks(paths: list[Path]) -> tuple[int, int, int]` returning `(deleted, failed, bytes_freed)`. For each path: if it exists, sum its `st_size` into `bytes_freed` then `unlink()`; count `deleted`. A missing path is a silent no-op (idempotent). An `OSError` on a present file increments `failed` (best-effort; never raises). Runs unlinks in the same dedicated executor `validate_chunks` uses (offload blocking FS ops).

- [ ] **Step 1: Write the failing test**

```python
async def test_purge_chunks_deletes_present_counts_bytes(tmp_path):
    from orchestrator.validator.disk_stat import purge_chunks
    a = tmp_path / "a"; a.write_bytes(b"12345")      # 5 bytes
    b = tmp_path / "b"; b.write_bytes(b"67")          # 2 bytes
    missing = tmp_path / "gone"
    deleted, failed, freed = await purge_chunks([a, b, missing])
    assert (deleted, failed, freed) == (2, 1 if False else 0, 7)  # missing is a no-op, not a failure
    assert not a.exists() and not b.exists()

async def test_purge_chunks_empty():
    from orchestrator.validator.disk_stat import purge_chunks
    assert await purge_chunks([]) == (0, 0, 0)
```

- [ ] **Step 2: Run to verify it fails** — `.venv/bin/python -m pytest tests/validator/test_disk_stat.py -k purge -v` → FAIL (`purge_chunks` undefined).

- [ ] **Step 3: Implement** — add to `disk_stat.py`, following `validate_chunks`'s executor-offload pattern:

```python
async def purge_chunks(paths: list[Path]) -> tuple[int, int, int]:
    """Unlink each present cache-chunk path. Returns (deleted, failed, bytes_freed).
    A missing path is a no-op (idempotent); an OSError on a present file counts as
    'failed' and is swallowed (best-effort — re-prefill is the safety net)."""
    def _purge() -> tuple[int, int, int]:
        deleted = failed = freed = 0
        for p in paths:
            try:
                st = p.stat()
            except OSError:
                continue  # not present → idempotent no-op
            try:
                freed += st.st_size
                p.unlink()
                deleted += 1
            except OSError:
                failed += 1
        return (deleted, failed, freed)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_cache_stat_executor(), _purge)
```

- [ ] **Step 4: Run to verify it passes** — same command → PASS.
- [ ] **Step 5: Commit** — `git add src/orchestrator/validator/disk_stat.py tests/validator/test_disk_stat.py && git commit -m "feat(purge): purge_chunks disk primitive"`

---

### Task 2: Agent `POST /v1/steam/purge`

**Files:**
- Modify: `src/orchestrator/agent/routers/steam.py` (add the purge endpoint next to the steam validate endpoint)
- Test: `tests/agent/test_steam_purge.py`

**Interfaces:**
- Consumes: `purge_chunks` (Task 1). The SAME manifest→chunk-path enumeration `POST /v1/steam/validate` already performs (`locate_manifest_bins` / `parse_chunk_shas` / `parse_shas` → `cache_path`). Refactor that enumeration into a shared helper `_steam_chunk_paths(settings, app_id) -> list[Path]` in `steam.py` so validate and purge share it (DRY — do NOT duplicate the manifest walk).
- Produces: `POST /v1/steam/purge` body `{app_id: int}` → `{deleted, failed, bytes_freed}`. Path-safety guard applied before unlink.

- [ ] **Step 1: Write the failing test** — build a fake cache root + fake manifest so the enumeration yields two chunk paths; write both files; POST purge; assert `deleted == 2`, files gone, and a bogus/outside path is never touched. Mirror `tests/agent/test_epic_validate.py`'s Settings + TestClient setup.

```python
def test_steam_purge_deletes_enumerated_chunks(tmp_path, monkeypatch):
    # ...seed manifest cache + cache_root so _steam_chunk_paths(app_id=440) -> [p1, p2]; write p1,p2...
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers={"Authorization": "Bearer " + "a"*32})
    r = client.post("/v1/steam/purge", json={"app_id": 440})
    assert r.status_code == 200
    assert r.json()["deleted"] == 2
    assert not p1.exists() and not p2.exists()
```

- [ ] **Step 2: Run to verify it fails** — endpoint 404 / helper undefined.
- [ ] **Step 3: Implement** — extract `_steam_chunk_paths`, add:

```python
class PurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: int

@router.post("/v1/steam/purge")
async def steam_purge(body: PurgeRequest, request: Request) -> dict[str, int]:
    settings = request.app.state.settings
    paths = _steam_chunk_paths(settings, body.app_id)
    safe = _under_cache_root(settings, paths)      # path-safety guard (shared helper, Task 2b)
    deleted, failed, freed = await purge_chunks(safe)
    return {"deleted": deleted, "failed": failed, "bytes_freed": freed}
```

- [ ] **Step 4: Run to verify it passes.**
- [ ] **Step 5: Commit.**

**Task 2b (folded in): path-safety helper.** Add `_under_cache_root(settings, paths) -> list[Path]` to a shared spot (`validator/cache_key.py` or `agent/_paths.py`): keep only paths where `p.resolve()` is a descendant of `settings.lancache_nginx_cache_path.resolve()`; log + drop any that aren't. Unit-test it directly with a traversal attempt (`cache_root/../../etc/passwd`) → dropped. This guard is used by both Steam and Epic purge.

---

### Task 3: Agent `POST /v1/epic/purge`

**Files:**
- Modify: `src/orchestrator/agent/routers/epic.py`
- Test: `tests/agent/test_epic_purge.py`

**Interfaces:**
- Consumes: `purge_chunks` (Task 1), `_under_cache_root` (Task 2b), and the SAME manifest parse→chunk-path enumeration `POST /v1/epic/validate` performs (`parse_manifest` / `chunk_path` / `epic_chunk_uri` / `cache_key` / `cache_path`). Refactor that into `_epic_chunk_paths(settings, app_id, version, cdn_base, raw_manifest_bytes) -> list[Path]` shared by validate + purge.
- Produces: `POST /v1/epic/purge` body `{app_id, version, cdn_base, raw_manifest_b64}` (same shape as epic validate) → `{deleted, failed, bytes_freed}`.

- [ ] Steps mirror Task 2 (test with a seeded Epic manifest + cache files → purge → deleted count + files gone + outside path untouched). Commit.

---

### Task 4: `AgentClient.steam_purge` + `epic_purge`

**Files:**
- Modify: `src/orchestrator/clients/agent_client.py` (add next to `steam_validate` / `epic_validate`)
- Test: `tests/clients/test_agent_client.py`

**Interfaces:**
- Produces:
  - `async def steam_purge(self, app_id: int) -> dict[str, Any]` → POSTs `/v1/steam/purge`, generous timeout (`httpx.Timeout(300.0, connect=10.0)` like steam_validate), returns the JSON.
  - `async def epic_purge(self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str) -> dict[str, Any]` → POSTs `/v1/epic/purge`. Same shape as `epic_validate`.

- [ ] TDD with an `httpx.MockTransport` (mirror the existing `steam_validate`/`epic_validate` client tests): assert the method/path/body and that it returns the parsed dict; assert a non-2xx raises `AgentError`. Commit.

---

### Task 5: Migration — extend `jobs.kind` CHECK to add `'purge'`

**Files:**
- Create: `src/orchestrator/db/migrations/0014_jobs_kind_purge.sql` (next free id — verify with `ls migrations/`)
- Modify: `src/orchestrator/db/migrations/CHECKSUMS`
- Test: `tests/db/test_migration_0014_purge_kind.py`

**Interfaces:**
- Produces: `jobs.kind` CHECK now includes `'purge'`; all existing rows/indexes preserved.

- [ ] **Step 1: Write the failing test** (aiosqlite, ADR-0001-compliant, mirror `test_migration_0012_gameshelf_source.py`): apply migrations through the prior one to `:memory:`, insert a `jobs` row with `kind='validate'`, then apply 0014, assert a `kind='purge'` insert succeeds and `kind='bogus'` still raises `IntegrityError`, and the pre-existing row survived.
- [ ] **Step 2: Verify fail** (`purge` rejected before the migration file exists).
- [ ] **Step 3: Implement** — copy `0009`'s structure exactly (it's the current `jobs` table shape): `ALTER TABLE jobs RENAME TO jobs_old;` → `CREATE TABLE jobs (...same columns/constraints... kind ... CHECK (kind IN ('prefill','validate','library_sync','auth_refresh','sweep','manifest_fetch','fetch_manifests','purge')));` → recreate every index that was on `jobs` (copy the `CREATE INDEX`/partial-unique statements from 0001/0004/0005/0006/0007/0009) → `INSERT INTO jobs (...cols...) SELECT ...cols... FROM jobs_old;` → `DROP TABLE jobs_old;`. **Read `0009` and the current `jobs` schema first and reproduce them faithfully** — a dropped index or column is a data-loss regression.
- [ ] **Step 4: Regenerate the checksum** — `shasum -a 256 src/orchestrator/db/migrations/0014_jobs_kind_purge.sql`, add the `0014  <sha>  0014_jobs_kind_purge.sql` line to `CHECKSUMS`.
- [ ] **Step 5: Run the migration test + full DB suite** (`pytest tests/db -q`) — every fixture migrates; PASS. Commit.

---

### Task 6: Purge job handler (control) + register the `purge` kind

**Files:**
- Create: `src/orchestrator/jobs/handlers/purge.py`
- Modify: the jobs worker's handler registry (wherever `validate_handler` is registered by `kind`)
- Test: `tests/jobs/test_purge_handler.py`

**Interfaces:**
- Consumes: `AgentClient.steam_purge` / `epic_purge` (Task 4). For Epic it must load `version`, `cdn_base`, and the raw manifest from the DB exactly as `validate_handler`'s Epic branch does (reuse that code path — read it and mirror it).
- Produces: `async def purge_handler(job: dict[str, Any], deps: Deps) -> None`. Dispatch on `job['platform']`: steam → `agent.steam_purge(app_id)`; epic → `agent.epic_purge(...)`. On result: set `games.status='validation_failed'` (WHERE id=game_id), and `_log.info("game.purged", game_id=..., platform=..., app_id=..., files_deleted=..., files_failed=..., total_bytes_freed=...)`. On `AgentError`: fail the job cleanly (mirror validate_handler's error handling), do NOT set validation_failed. Idempotent: a `{deleted:0}` result still sets `validation_failed` (harmless — re-validate confirms) — but skip the status write if the game is already `validation_failed` to avoid churn.

- [ ] TDD: a fake `deps.agent_client` returning `{deleted:3, failed:0, bytes_freed:999}`; assert the game's status becomes `validation_failed` and the log event fired (capture with structlog testing). A steam and an epic case. An `AgentError` case → job fails, status unchanged. Commit.

---

### Task 7: API `POST /api/v1/games/{platform}/{app_id}/purge`

**Files:**
- Create: `src/orchestrator/api/routers/purge_trigger.py` (mirror `prefill_trigger.py` / `validate_trigger.py`)
- Modify: `src/orchestrator/api/main.py` (import + `include_router`)
- Test: `tests/api/test_purge_trigger_router.py`

**Interfaces:**
- Consumes: the `purge` job kind (Task 5). Produces: `POST /api/v1/games/{platform}/{app_id}/purge` (bearer-auth). Looks up the game by (platform, app_id); guard `platform in ("steam","epic")` → 400 otherwise; INSERT a `jobs` row `(kind='purge', game_id, platform, state='queued', source='api')` with the in-flight dedup (`ON CONFLICT DO NOTHING`, mirroring prefill_trigger). Returns `202 {job_id}`. A game not found → 404. (Idempotency of the *delete* lives in the agent; the API just enqueues.)

- [ ] TDD (mirror `test_prefill_trigger_router.py`): steam game → 202 + a `purge` job row with `platform='steam'`; epic game → 202 + `platform='epic'`; unknown platform (seed a gog game) → 400; missing token → 401; game-not-found → 404. Commit.

---

### Task 8: CLI `orchestrator-cli game <platform>/<app_id> purge`

**Files:**
- Modify: `src/orchestrator/cli/commands/game.py` (add `purge` next to `prefill` / `validate`)
- Test: `tests/cli/test_game_commands.py`

**Interfaces:**
- Produces: `game purge <platform>/<app_id>` → POSTs the API endpoint over loopback, prints the queued job id. Mirror the existing `game validate` command exactly (arg parsing, base URL, output).

- [ ] TDD (mirror the `game validate` CLI test with a mocked API response): invoking the command hits `POST /api/v1/games/steam/440/purge` and prints the job id. Commit.

---

### Task 9: Security audit + docs + deploy note + record feature

**Files:**
- Create: `docs/security-audits/f18-cache-purge-security-audit.md`
- Modify: `CHANGELOG.md` (Security + Added + Data Model + Infrastructure entries), `docs/ADR documentation/0015-operator-driven-cache-purge.md` (flip a "implemented" note if desired)

- [ ] **Security audit** (Senior Security Engineer persona) — the threat is *deleting the wrong files*. Verify: 32-hex hash validation at the agent; `_under_cache_root` resolve-guard rejects traversal (with a test payload `cache_root/../../etc`); bearer-auth on the API; the delete is bounded to a single game's enumerated chunks; reversibility (re-prefill). No SQL injection (params bound). No cache-wide/arbitrary delete path. Record findings (expect none) + the RW-mount trade-off from ADR-0015.
- [ ] **CHANGELOG:** Security (agent cache mount relaxed :ro→:rw — bounded by path guards), Data Model (migration 0014 jobs.kind += purge), Added (purge API/CLI/agent endpoints), Infrastructure (deploy-agent.sh mount change).
- [ ] **Deploy note (in the PR body + CHANGELOG):** the agent container's cache mount changes `:ro → :rw` — the agent redeploy (`deploy-agent.sh`) must update the mount; the LXC redeploy is code-only. Verify post-deploy: `docker inspect orchestrator-agent` shows `RW=true` for `/data/cache`, then a live purge of a small test game → validate shows the chunks missing → prefill heals → validate green.
- [ ] `scripts/test-gate.sh --record-feature "f18-cache-purge"` + complete the build-loop steps. Full suite green. Commit. Open the orchestrator PR.

---

### Task 10: Game_shelf "Delete from cache" button (SEPARATE repo + PR)

**Files (repo `/Users/karl/Documents/Claude Projects/Game_shelf`, branch `feat/f18-purge-button` off master):**
- Modify: `backend/src/routes/cache.js` (add `POST /api/cache/games/:id/purge` proxy — mirror the existing `validate` / `prefill` forwards to `/api/v1/games/{id}/purge`... note the orchestrator route is keyed by `{platform}/{app_id}`, so the proxy must resolve those from the game — mirror however the existing prefill/validate proxies map `:id`)
- Modify: `frontend/src/pages/GameDetail.jsx` (add a "Delete from cache" button behind a confirm dialog, next to the existing "Complete Re-download" / cache actions)
- Test: `backend/tests/routes/cache.test.js` (proxy test, node:test) + a frontend test if the existing cache buttons have one (vitest)

**Interfaces:**
- Consumes: the orchestrator `POST /api/v1/games/{platform}/{app_id}/purge` (Task 7). The Game_shelf backend already knows the launcher/app_id for a game (see how `crossLauncherExclusions` / the cache proxies derive them).
- Produces: a confirm-gated "Delete from cache" action; on confirm → `fetch` the proxy with `credentials:'same-origin'` → `queryClient.invalidateQueries(['game', id])` + `['games']` (match the existing GameDetail mutation pattern — plain fetch + invalidate, no useMutation).

- [ ] TDD the backend proxy (mock orchestrator, assert method/path/status pass-through + auth 401). Wire the button with a `window.confirm`-style dialog matching the existing destructive-action affordance. Full backend suite (`node --test 'backend/tests/**/*.test.js'`) — no NEW failures beyond the 2 pre-existing. Commit. Open the Game_shelf PR.

---

## Self-Review

**Spec coverage (vs ADR-0015 + #37 acceptance criteria):**
- API endpoint ✓ (Task 7) · CLI ✓ (Task 8) · UI ✓ (Task 10) · behavior/enumerate→unlink→validation_failed ✓ (Tasks 1-3,6) · audit `purge` job + migration ✓ (Tasks 5,6) · idempotency ✓ (Task 1 no-op + Task 7) · block-list interaction ✓ (Global Constraints — purge never touches block_list; re-prefill gated) · observability `game.purged` ✓ (Task 6) · Lancache safety/Spike G ✓ (done in ADR-0015) · RW-mount + path safety ✓ (Task 2b, 9).
- Non-goals honored: per-game only, no cache-wide, no chunk-level, no bit-integrity scheduler.

**Placeholder scan:** the two enumeration helpers (`_steam_chunk_paths`, `_epic_chunk_paths`) are refactor-extractions of existing validate code — the implementer must READ the current steam/epic validate endpoints and lift the enumeration verbatim, not re-invent it. Task 5 requires faithfully reproducing the current `jobs` schema + indexes (read 0009 + 0001) — the single highest-risk step.

**Type consistency:** agent returns `{deleted, failed, bytes_freed}` everywhere (Tasks 1-4,6); the API returns `{job_id}` (202) and the eventual purge result is `{deleted}` at the job level; CLI/UI consume the 202. Consistent.

**Ordering:** Tasks 1→9 are the orchestrator PR (agent + control + API + CLI). Task 10 is a separate Game_shelf PR that depends on Task 7 being deployed. Deploy the orchestrator (incl. the `:ro→:rw` agent mount) before the Game_shelf button is useful end-to-end.
