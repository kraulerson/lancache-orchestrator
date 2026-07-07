# Steam New-Purchase Auto-Coverage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A newly-purchased Steam game (downloaded by the host `SteamPrefill --recently-purchased` cron) automatically gets validated (status off `unknown`), gets a durable `.shas` sidecar, and persists into the SteamPrefill selection — with no manual step.

**Architecture:** Three independent orchestrator-side changes: (1) the gated validation sweep enumerates `unknown` owned games; (2) the manifest fetcher also covers apps that have a `.bin` in the cache but no `.shas`; (3) the selection reconcile re-adds prefilled-non-excluded apps. No host-cron edit, no agent RPC change (Piece 3 reuses `restore_ids`), no 2FA, no schema change.

**Tech Stack:** Python 3.12, asyncio, aiosqlite pool, FastAPI agent, pytest/mypy/ruff.

## Global Constraints

- **Repo:** `/Users/karl/Documents/Claude Projects/lancache_orchestrator`, branch `feat/steam-new-purchase-auto-coverage` (created, spec committed). Framework hooks ACTIVE — before editing any source file, a plan task must be `in_progress` (TaskUpdate); new third-party imports need a Context7 lookup first (none expected here); pre-commit runs ruff+mypy+semgrep.
- **Do NOT** populate `games.metadata`/`current_version` for steam (vestigial). **Do NOT** add a near-immediate `library_sync` validate-enqueue (immediacy = ≤6h, locked). **Do NOT** change the `fetch_manifests` cadence (weekly, locked). **Do NOT** edit the host cron / `selectedAppsToPrefill.json` host-side. **No** new agent selection RPC.
- **Import isolation:** the agent package (`src/orchestrator/agent/**`, `platform/steam/manifest_fetcher.py`) must NOT import `orchestrator.db.*` / `orchestrator.api.*` — `tests/agent/test_import_isolation.py` enforces this. Inline the manifest glob; do not import `manifest_locator` if it would pull disallowed deps (it's in `agent/` so it's safe to reference for the pattern, but inline to stay minimal).
- **Verify commands:** `.venv/bin/python -m pytest <path> -q`; full suite `.venv/bin/python -m pytest -q --ignore=tests/scripts`; `.venv/bin/python -m mypy src` ; `.venv/bin/ruff check src tests`.
- One PR. Karl merges (never `gh pr merge`).

## File map
- `src/orchestrator/jobs/handlers/sweep.py` — Piece 1 (`_CANDIDATE_SQL`).
- `src/orchestrator/platform/steam/manifest_fetcher.py` — Piece 2 (`__init__` + `_enumerate_app_ids`).
- `src/orchestrator/agent/app.py` — Piece 2 wiring (two fetcher constructions).
- `src/orchestrator/scheduler/jobs.py` — Piece 3 (`auto_classify_block` restore set).
- `CHANGELOG.md`, `FEATURES.md` — docs.

---

### Task 1: Piece 1 — sweep validates `unknown` owned games

**Files:**
- Modify: `src/orchestrator/jobs/handlers/sweep.py` (`_CANDIDATE_SQL`, ~line 25)
- Test: `tests/jobs/test_sweep_handler.py`

**Interfaces:**
- Produces: the gated sweep's candidate set now includes `status='unknown' AND owned=1` steam/epic games. `_CANDIDATE_SQL_FULL` unchanged.

Current code (`sweep.py`):
```python
_CANDIDATE_SQL = (
    "SELECT id, status FROM games WHERE status IN ('up_to_date','validation_failed') ORDER BY id"
)
```

- [ ] **Step 1: Write the failing test** — add to `tests/jobs/test_sweep_handler.py` (mirror the existing harness in that file — it builds a Deps with a real/temp pool + a stub agent_client and a patched `validator_self_test`/`validate_one_game`; copy that fixture setup). Assert the gated sweep enumerates an `unknown, owned=1` game and skips an `unknown, owned=0` game:

```python
async def test_gated_sweep_includes_unknown_owned_game(...):
    # seed games: A(status='unknown',owned=1), B(status='unknown',owned=0),
    #             C(status='up_to_date',owned=1)
    # run sweep_handler with an empty payload (gated), capturing validate_one_game calls
    validated_ids = [...]  # collected from the patched validate_one_game
    assert A_id in validated_ids
    assert C_id in validated_ids
    assert B_id not in validated_ids     # owned=0 unknown is NOT swept
```

Add a second test asserting a no-manifest `unknown` game's error does not clobber status (mirror how the file already asserts non-clobbering / uses `validate_one_game` returning `outcome='error'`):
```python
async def test_uncovered_unknown_stays_unknown(...):
    # validate_one_game for the unknown game raises/returns outcome='error'
    # assert the game's status is still 'unknown' after the sweep (non-clobbering)
```

- [ ] **Step 2: Run to verify it fails** — `.venv/bin/python -m pytest tests/jobs/test_sweep_handler.py -q` → the new test FAILS (`unknown` game not enumerated).
- [ ] **Step 3: Implement** — in `sweep.py`:
```python
_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE status IN ('unknown','up_to_date','validation_failed') AND owned = 1 "
    "ORDER BY id"
)
```
(The `owned = 1` guard bounds churn — only owned games are swept. `validate_one_game` already leaves status unchanged on `outcome='error'`, so an uncovered `unknown` game is safe.)

- [ ] **Step 4: Run to verify it passes** — same command → PASS; also run the file's existing gated + full sweep tests → still green.
- [ ] **Step 5: Commit**
```bash
git add src/orchestrator/jobs/handlers/sweep.py tests/jobs/test_sweep_handler.py
git commit -m "fix(sweep): gated sweep validates 'unknown' owned games (auto-cover new purchases)"
```

---

### Task 2: Piece 2 — fetcher covers has-.bin-but-no-.shas apps

**Files:**
- Modify: `src/orchestrator/platform/steam/manifest_fetcher.py` (`__init__` + `_enumerate_app_ids`)
- Test: `tests/platform/steam/test_manifest_fetcher.py` (mirror the existing path — check the actual test file location for the fetcher; adjust if it lives elsewhere)

**Interfaces:**
- Consumes: `settings.steam_manifest_cache_dir` (`/steamprefill-cache`, the live `.bin` cache), `steam_manifest_archive_dir` (`/manifest-archive`, where `.shas` are written).
- Produces: `DepotDownloaderManifestFetcher(..., manifest_cache_dir: Path | None = None)`. `_enumerate_app_ids()` returns `sorted(selection ∪ {apps with a .bin in manifest_cache_dir/v1 and no .shas in archive_dir/v1})`.

Current `__init__` signature keywords: `binary, config_dir, steam_config_dir, archive_dir, delay_sec=0.0, username="", max_retries=3, retry_backoff_sec=15.0`. Current `_enumerate_app_ids` reads `self._steam_config_dir / "selectedAppsToPrefill.json"` (a JSON list of ints). Manifest filenames are `{app}_{...}.{bin|shas}` under `<dir>/v1/`; the app_id is `stem.split("_",1)[0]` when `.isdigit()`.

- [ ] **Step 1: Write the failing test** — add to the fetcher test:
```python
def test_enumerate_unions_uncovered_cache_apps(tmp_path):
    steam_cfg = tmp_path / "steamcfg"; steam_cfg.mkdir()
    (steam_cfg / "selectedAppsToPrefill.json").write_text("[111]")   # selection
    cache = tmp_path / "cache"; (cache / "v1").mkdir(parents=True)
    archive = tmp_path / "archive"; (archive / "v1").mkdir(parents=True)
    # 222 has a .bin but no .shas -> must be enumerated (a recent purchase)
    (cache / "v1" / "222_222_2221_gidA.bin").write_bytes(b"x")
    # 333 has a .bin AND a matching .shas -> already covered, must be skipped
    (cache / "v1" / "333_333_3331_gidB.bin").write_bytes(b"x")
    (archive / "v1" / "333_333_3331_gidB.shas").write_text("")
    f = DepotDownloaderManifestFetcher(
        binary=tmp_path/"dd", config_dir=tmp_path/"cfg", steam_config_dir=steam_cfg,
        archive_dir=archive, manifest_cache_dir=cache,
    )
    assert f._enumerate_app_ids() == [111, 222]   # selection + uncovered; NOT 333

def test_enumerate_selection_only_when_no_cache_dir(tmp_path):
    steam_cfg = tmp_path / "steamcfg"; steam_cfg.mkdir()
    (steam_cfg / "selectedAppsToPrefill.json").write_text("[111]")
    f = DepotDownloaderManifestFetcher(
        binary=tmp_path/"dd", config_dir=tmp_path/"cfg", steam_config_dir=steam_cfg,
        archive_dir=tmp_path/"a", manifest_cache_dir=None,
    )
    assert f._enumerate_app_ids() == [111]
```

- [ ] **Step 2: Run to verify it fails** — `.venv/bin/python -m pytest tests/platform/steam/test_manifest_fetcher.py -q` → FAIL (`__init__` has no `manifest_cache_dir`; selection-only enumeration).
- [ ] **Step 3: Implement** — in `manifest_fetcher.py`:

Add the param to `__init__` (keyword-only, after `retry_backoff_sec`):
```python
        retry_backoff_sec: float = 15.0,
        manifest_cache_dir: Path | None = None,
    ) -> None:
        ...
        self._retry_backoff_sec = retry_backoff_sec
        self._manifest_cache_dir = Path(manifest_cache_dir) if manifest_cache_dir else None
```

Add an inlined glob helper + widen `_enumerate_app_ids` (keep the existing selection parse; append the union):
```python
    @staticmethod
    def _app_ids_with_ext(directory: Path | None, ext: str) -> set[int]:
        """app_ids that have a `<dir>/v1/{app}_*.{ext}` manifest. Inlined (no
        manifest_locator import) to preserve agent import-isolation."""
        apps: set[int] = set()
        if directory is None:
            return apps
        v1 = directory / "v1"
        if not v1.is_dir():
            return apps
        for path in v1.glob(f"*.{ext}"):
            first = path.stem.split("_", 1)[0]
            if first.isdigit():
                apps.add(int(first))
        return apps
```
At the end of `_enumerate_app_ids`, replace `return sorted(apps)` with:
```python
        # Durability (#213 follow-up): also cover apps that were prefilled outside
        # the selection (e.g. a --recently-purchased game) — a `.bin` in the live
        # cache with no `.shas` in the archive yet. Bounded to that delta so the
        # first run never triggers a full-library DepotDownloader logon burst (#228).
        have_bin = self._app_ids_with_ext(self._manifest_cache_dir, "bin")
        have_shas = self._app_ids_with_ext(self._archive_dir, "shas")
        uncovered = have_bin - have_shas
        return sorted(apps | uncovered)
```
(The existing `delay_sec` throttle at the `fetch_all` loop and `_TRANSIENT_RE` backoff are untouched — they still bound Steam-logon rate.)

- [ ] **Step 4: Run to verify it passes** — same command → PASS. Then `.venv/bin/python -m pytest tests/agent/test_import_isolation.py -q` → still green (no new imports).
- [ ] **Step 5: Commit**
```bash
git add src/orchestrator/platform/steam/manifest_fetcher.py tests/platform/steam/test_manifest_fetcher.py
git commit -m "feat(fetcher): also cover prefilled apps missing a .shas (durable coverage for new purchases)"
```

---

### Task 3: Piece 2 wiring — pass `manifest_cache_dir` in the agent

**Files:**
- Modify: `src/orchestrator/agent/app.py` (two `DepotDownloaderManifestFetcher(...)` constructions, ~lines 72-81 and 118-127)
- Test: `tests/agent/test_app.py` (or wherever agent-app construction is tested; else add a focused test)

**Interfaces:**
- Consumes: `DepotDownloaderManifestFetcher(manifest_cache_dir=...)` (Task 2).
- Produces: both fetcher instances built with `manifest_cache_dir=settings.steam_manifest_cache_dir`.

- [ ] **Step 1: Write the failing test** — assert the constructed fetcher carries the cache dir (mirror any existing agent-app construction test; if none, build the app/state and check `app.state.manifest_fetcher._manifest_cache_dir == settings.steam_manifest_cache_dir`). Keep it minimal.
- [ ] **Step 2: Run to verify it fails** — FAIL (`_manifest_cache_dir` is None).
- [ ] **Step 3: Implement** — in BOTH constructions in `agent/app.py`, add the line after `retry_backoff_sec=...`:
```python
                retry_backoff_sec=settings.manifest_fetch_retry_backoff_sec,
                manifest_cache_dir=settings.steam_manifest_cache_dir,
            )
```
- [ ] **Step 4: Run to verify it passes** — PASS.
- [ ] **Step 5: Commit**
```bash
git add src/orchestrator/agent/app.py tests/agent/test_app.py
git commit -m "feat(agent): wire steam_manifest_cache_dir into the manifest fetcher"
```

---

### Task 4: Piece 3 — selection reconcile re-adds prefilled-non-excluded apps

**Files:**
- Modify: `src/orchestrator/scheduler/jobs.py` (`auto_classify_block`, the block that computes `restore_ids` and calls `agent_client.prune_steam_selection`)
- Test: `tests/scheduler/test_jobs.py`

**Interfaces:**
- Consumes: `agent_client.prefilled_apps() -> list[int]`, `agent_client.prune_steam_selection(exclude_app_ids, restore_app_ids)`.
- Produces: `restore_ids` sent to the agent = `(DB allow set) ∪ (prefilled_apps() − DB exclude set)`. Best-effort; a `prefilled_apps()` error leaves `restore_ids` as the allow set only.

Current code (`scheduler/jobs.py`, in `auto_classify_block`):
```python
            excl = await pool.read_all("SELECT app_id FROM prefill_exclusions WHERE platform = 'steam' AND mode = 'exclude'")
            allow = await pool.read_all("SELECT app_id FROM prefill_exclusions WHERE platform = 'steam' AND mode = 'allow'")
            exclude_ids = [i for i in (as_int(r["app_id"]) for r in excl) if i is not None]
            restore_ids = [i for i in (as_int(r["app_id"]) for r in allow) if i is not None]
            if exclude_ids or restore_ids:
                res = await agent_client.prune_steam_selection(exclude_ids, restore_ids)
```

- [ ] **Step 1: Write the failing test** — add to `tests/scheduler/test_jobs.py` (mirror its existing `auto_classify_block` test harness: a fake pool + a stub `agent_client` recording `prune_steam_selection` args). Seed `prefill_exclusions`: 900='exclude'. Stub `prefilled_apps() -> [648800, 900]`:
```python
async def test_reconcile_readds_prefilled_non_excluded():
    # DB: 900 excluded (mode='exclude'); agent prefilled_apps -> [648800, 900]
    ...
    call = stub_agent.prune_calls[-1]      # (exclude_ids, restore_ids)
    assert 648800 in call.restore_ids      # prefilled + not excluded -> re-added
    assert 900 not in call.restore_ids     # excluded -> not re-added
    assert 900 in call.exclude_ids

async def test_reconcile_survives_prefilled_apps_error():
    # prefilled_apps() raises -> restore_ids falls back to the DB allow set, no raise
    ...
```

- [ ] **Step 2: Run to verify it fails** — `.venv/bin/python -m pytest tests/scheduler/test_jobs.py -q` → FAIL (648800 not in restore_ids).
- [ ] **Step 3: Implement** — replace the `restore_ids` computation with the union (keep everything inside the existing best-effort `try`):
```python
            restore_ids = [i for i in (as_int(r["app_id"]) for r in allow) if i is not None]
            # #NEW: recently-purchased/prefilled games (a `.bin` in the agent cache)
            # that are not excluded should be (re)added to the selection so they
            # persist in --select-apps and the durable prefill set. prefilled_apps()
            # is the same .bin-cache source library_sync uses.
            try:
                prefilled = await agent_client.prefilled_apps()
            except Exception as e:
                prefilled = []
                _log.warning("scheduler.auto_classify_block.prefilled_apps_failed", reason=str(e)[:200])
            exclude_set = set(exclude_ids)
            restore_ids = sorted({*restore_ids, *(a for a in prefilled if a not in exclude_set)})
            if exclude_ids or restore_ids:
                res = await agent_client.prune_steam_selection(exclude_ids, restore_ids)
```
(`_log` is already imported in the module. The whole block is already inside `try/except Exception` so a failure never crashes the scheduler tick.)

- [ ] **Step 4: Run to verify it passes** — same command → PASS; existing `auto_classify_block` tests → green.
- [ ] **Step 5: Commit**
```bash
git add src/orchestrator/scheduler/jobs.py tests/scheduler/test_jobs.py
git commit -m "feat(selection): reconcile re-adds prefilled-non-excluded apps (recent purchases persist in the selection)"
```

---

### Task 5: Docs + governance

**Files:** `CHANGELOG.md`, `FEATURES.md` (+ process scripts).

- [ ] **Step 1** — CHANGELOG.md, under a new dated entry:
  - **Fixed:** "Newly-purchased Steam games stayed `unknown` forever — the gated validation sweep never enumerated `unknown` games (and there is no scheduled full sweep). The sweep now validates `unknown` owned games, so a recent purchase is auto-validated within one 6h cycle off its already-cached manifest."
  - **Changed:** "The Steam manifest fetcher now also covers apps that have a live `.bin` but no durable `.shas` (recent purchases outside the SteamPrefill selection), bounded to that delta. The selection reconcile now re-adds prefilled-non-excluded apps to `selectedAppsToPrefill.json` so recent purchases persist in `--select-apps` and the durable prefill set."
- [ ] **Step 2** — FEATURES.md: note the new-purchase auto-coverage behavior.
- [ ] **Step 3** — run the build-loop process checklist per CLAUDE.md (`scripts/process-checklist.sh --complete-step build_loop:...` for the steps completed) and `scripts/test-gate.sh --record-feature "steam-new-purchase-auto-coverage"`.
- [ ] **Step 4: Commit**
```bash
git add CHANGELOG.md FEATURES.md
git commit -m "docs: steam new-purchase auto-coverage (sweep unknown + fetcher + selection persist)"
```

---

### Task 6: Full verification + PR + deploy + live verify

**Files:** none (verification/deploy only).

- [ ] **Step 1: Full suite** — `.venv/bin/python -m pytest -q --ignore=tests/scripts` (only pre-existing failures, e.g. `test_licenses.py`); `.venv/bin/python -m mypy src`; `.venv/bin/ruff check src tests`.
- [ ] **Step 2: Push + PR**
```bash
git push -u origin feat/steam-new-purchase-auto-coverage
gh pr create --base main --title "feat: auto-cover newly-purchased Steam games (sweep unknown + fetcher + selection persist)" --body "..."
```
PR body: the 3 pieces + the Raft root-cause; **no schema change, no host-cron edit, no 2FA**; per spec `docs/superpowers/specs/2026-07-07-steam-new-purchase-auto-coverage-design.md`. Karl merges.
- [ ] **Step 3: Deploy (after merge, no 2FA).** Control plane (Pieces 1+3) → LXC 1105: tag rollback `docker tag orchestrator:dpa orchestrator:dpa-pre-newpurchase`; then `cd /root/lancache-orchestrator && git fetch && git reset --hard origin/main && docker build -t orchestrator:dpa . && bash /root/deploy-orchestrator-lxc.sh` (build FIRST — the script only recreates). Agent (Piece 2) → UGREEN: rebuild the agent image and **recreate** the agent container (not restart — env reload).
- [ ] **Step 4: Live verify.** Within one 6h gated sweep, `games.status` for `648800` → `up_to_date` (query the LXC DB `/var/lib/orchestrator/orchestrator.db` via `python3` in the `orchestrator` container). After the next `auto_classify_block` tick, `648800` appears in `selectedAppsToPrefill.json` on the agent (host-root read on .40). On the next weekly `fetch_manifests`, a `648800_*_648801_*.shas` lands in the `/manifest-archive/v1` volume. Confirm `validation_sweep` + `fetch_manifests` scheduled jobs are enabled live. Confirm no regression on the other legacy `unknown` rows (validate or stay `unknown` via non-clobbering error).

---

## Self-Review

**Spec coverage:** Piece 1 (sweep unknown) → Task 1 ✓; Piece 2 (fetcher widen) → Tasks 2+3 ✓; Piece 3 (selection persist) → Task 4 ✓; docs → Task 5 ✓; deploy+verify → Task 6 ✓. Non-goals respected — no `games.metadata` write, no `library_sync` validate-enqueue, no cadence change, no host edit, no schema change, no new agent RPC.

**Placeholder scan:** implementation steps carry real code (the exact `_CANDIDATE_SQL`, the `_app_ids_with_ext` helper + enumeration union, the two `agent/app.py` lines, the `restore_ids` union). The handler/scheduler TEST bodies name the exact seed + assertions and point to the existing test file's fixture to mirror (the pool/Deps/stub-agent harness differs per file and must match what's there) — that's a harness-reuse instruction, not a placeholder.

**Type consistency:** `manifest_cache_dir: Path | None` (Task 2) matches the wiring `settings.steam_manifest_cache_dir` (Task 3) and the enumeration read (Task 2). `prefilled_apps() -> list[int]` and `prune_steam_selection(exclude_app_ids, restore_app_ids)` (Task 4) match the real `agent_client` signatures. `_CANDIDATE_SQL` string (Task 1) is the only sweep change; `_CANDIDATE_SQL_FULL` untouched.
