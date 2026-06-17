# F8 — Block List + Scheduled Prefill Driver — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **NO per-task commits.** Per the project workflow, implement ALL tasks TDD-style (write failing test → verify red → implement → verify green), then Task 13 does the single combined gate-sweep + security audit + adversarial-verify + docs + one `feat(f8)` commit + PR.

**Goal:** Make the orchestrator the automatic Steam+Epic prefill driver — a 6h cycle that prefills only changed/never-cached/validation-failed owned games (version-diff), with an operator block-list to exclude games.

**Architecture:** Populate the vestigial `games.current_version` (from library enumeration) and `cached_version` (on prefill/validate success). A new scheduler job diffs them and enqueues prefill only for divergent, non-block-listed games. A new `block_list` REST resource (table already exists) + CLI `game block/unblock` manage exclusions; existing cache is adopted via a widened validation sweep (stat-only, no re-download).

**Tech Stack:** Python 3.12, FastAPI, aiosqlite (via `orchestrator.db.pool`), APScheduler, Click, httpx, pydantic v2, structlog, pytest.

**Conventions (apply to every task):**
- Tests: `.venv/bin/pytest <path> -v`. Lint: `.venv/bin/ruff check` + `.venv/bin/ruff format`. Types: `.venv/bin/mypy --strict src/`.
- DB writes via `pool.execute_write(sql, params)`; reads via `pool.read_one` / `pool.read_all`. **No raw `sqlite3`; no f-string SQL with user values** — all values bound via `?`. SQL built from allow-list-validated field names carries `# noqa: S608` + a `# nosem` note (see `games.py:185-192`).
- **No DB migration** — `block_list` already exists in `0001_initial.sql`.
- `block_list` is the single source of truth — never mutate `games.status` on block/unblock.
- `cached_version` is written ONLY on full prefill success AND clean validation, set equal to `current_version`.
- Never echo/log credentials or tokens.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/orchestrator/platform/steam/enumerate.py` | emit a per-app `version` token (buildid / depot-gid composite) | 1 |
| `src/orchestrator/platform/epic/models.py` | `EpicLibraryItem.build_version` field | 2 |
| `src/orchestrator/platform/epic/library.py` | populate `build_version` in `_to_item` | 2 |
| `src/orchestrator/jobs/handlers/library_sync.py` | upsert `current_version` (steam + epic) | 3 |
| `src/orchestrator/jobs/handlers/prefill.py` | set `cached_version=current_version` on full success | 4 |
| `src/orchestrator/jobs/handlers/validate.py` | set `cached_version=current_version` on clean validation | 5 |
| `src/orchestrator/jobs/handlers/sweep.py` | widen candidate query for cold-start adoption | 6 |
| `src/orchestrator/scheduler/jobs.py` | new `enqueue_scheduled_prefill` (diff insert) | 7 |
| `src/orchestrator/scheduler/manager.py` | register the 6h scheduled-prefill job | 8 |
| `src/orchestrator/core/settings.py` | `scheduled_prefill_enabled` flag | 8 |
| `src/orchestrator/api/routers/block_list.py` | **new** GET/POST/DELETE block-list router | 9 |
| `src/orchestrator/api/main.py` | register the block-list router | 9 |
| `src/orchestrator/api/routers/games.py` | `blocked: bool` via EXISTS subquery | 10 |
| `src/orchestrator/cli/client.py` | `OrchClient.delete()` | 11 |
| `src/orchestrator/cli/commands/game.py` | `game block` / `game unblock` + `blocked` column | 11 |

---

## Task 1: Steam per-app version token

**Files:**
- Modify: `src/orchestrator/platform/steam/enumerate.py` (`_extract_app_metadata` ~line 113-144; add `_app_version_token` helper near `manifest_gids_for_app`)
- Test: `tests/platform/steam/test_enumerate.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/platform/steam/test_enumerate.py  (add)
from orchestrator.platform.steam.enumerate import _app_version_token, _extract_app_metadata


class TestAppVersionToken:
    def test_prefers_public_branch_buildid(self):
        depots = {"branches": {"public": {"buildid": "1788499"}}, "1234": {"manifests": {}}}
        assert _app_version_token(depots) == "1788499"

    def test_composite_when_no_buildid(self):
        # no branches.public.buildid -> deterministic hash of sorted depot:gid pairs
        depots = {"1234": {"manifests": {"public": {"gid": "555"}}},
                  "1200": {"manifests": {"public": {"gid": "777"}}}}
        tok = _app_version_token(depots)
        # same input (order-independent) -> same token
        assert tok == _app_version_token(
            {"1200": {"manifests": {"public": {"gid": "777"}}},
             "1234": {"manifests": {"public": {"gid": "555"}}}}
        )
        assert tok is not None and tok != "1788499"

    def test_none_when_no_version_info(self):
        assert _app_version_token({"branches": {"public": {}}}) is None

    def test_extract_app_metadata_includes_version(self):
        apps_response = {"apps": {"10": {
            "common": {"name": "Game"},
            "depots": {"branches": {"public": {"buildid": "42"}}, "11": {"manifests": {}}},
        }}}
        out = _extract_app_metadata(apps_response, [10])
        assert out == [{"app_id": 10, "name": "Game", "depots": [11], "version": "42"}]
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/platform/steam/test_enumerate.py -k "AppVersionToken or includes_version" -v`
Expected: FAIL — `ImportError: cannot import name '_app_version_token'`.

- [ ] **Step 3: Implement**

```python
# enumerate.py — add near manifest_gids_for_app (after extract_manifest_gid)
import hashlib

def _app_version_token(depots: Any) -> str | None:
    """A stable per-app version string. Prefers the public-branch buildid
    (changes on every app update); falls back to a SHA-256 of the sorted
    (depot_id, manifest_gid) pairs so any depot manifest change shifts it.
    Returns None when neither is available (game then treated as needing prefill)."""
    if isinstance(depots, dict):
        buildid = (((depots.get("branches") or {}).get("public") or {}).get("buildid"))
        if buildid is not None and str(buildid):
            return str(buildid)
    pairs = manifest_gids_for_app(depots, "public")
    if not pairs:
        return None
    joined = ",".join(f"{d}:{g}" for d, g in sorted(pairs))
    return hashlib.sha256(joined.encode()).hexdigest()
```

In `_extract_app_metadata`, change the append (was line ~143):

```python
        out.append({
            "app_id": int(app_id),
            "name": name,
            "depots": depot_ids,
            "version": _app_version_token(depots_dict),
        })
```

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/platform/steam/test_enumerate.py -v`
Expected: PASS (all, including pre-existing).

---

## Task 2: Epic `build_version` on the library item

**Files:**
- Modify: `src/orchestrator/platform/epic/models.py` (`EpicLibraryItem`), `src/orchestrator/platform/epic/library.py` (`_to_item` ~line 49)
- Test: `tests/platform/epic/test_library.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/platform/epic/test_library.py  (add)
from orchestrator.platform.epic.library import _to_item


def test_to_item_carries_build_version():
    rec = {
        "appName": "Fortnite", "title": "Fortnite",
        "namespace": "fn", "catalogItemId": "abc", "buildVersion": "++Fortnite-29.00",
    }
    item = _to_item(rec)
    assert item is not None
    assert item.build_version == "++Fortnite-29.00"


def test_to_item_build_version_optional():
    rec = {"appName": "X", "title": "X", "namespace": "n", "catalogItemId": "c"}
    item = _to_item(rec)
    assert item is not None and item.build_version is None
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/platform/epic/test_library.py -k build_version -v`
Expected: FAIL — `AttributeError: 'EpicLibraryItem' object has no attribute 'build_version'`.

- [ ] **Step 3: Implement**

In `models.py`, add to the `EpicLibraryItem` dataclass/model a field `build_version: str | None = None` (match the existing declaration style — if it's a `@dataclass`, add `build_version: str | None = None`; if pydantic `BaseModel`, add `build_version: str | None = None`).

In `library.py` `_to_item`, read the field from the asset record (key is `buildVersion`) and pass it when constructing the item:

```python
    build_version = rec.get("buildVersion")
    return EpicLibraryItem(
        # ...existing kwargs unchanged...
        build_version=(str(build_version) if build_version else None),
    )
```

(If `_to_item` currently returns `EpicLibraryItem(app_name=..., title=..., namespace=..., catalog_item_id=...)`, add the `build_version=` kwarg to that call.)

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/platform/epic/test_library.py -v`
Expected: PASS.

---

## Task 3: `library_sync` writes `current_version`

**Files:**
- Modify: `src/orchestrator/jobs/handlers/library_sync.py` (`_UPSERT_SQL` line 24-31; steam call line 142; epic call line 72)
- Test: `tests/jobs/test_library_sync_handler.py` (or the existing library-sync test module)

- [ ] **Step 1: Write the failing tests**

```python
# tests/jobs/test_library_sync_handler.py  (add — adapt to existing fixture names)
async def test_steam_sync_writes_current_version(pool, steam_deps_with_app):
    # steam_deps_with_app stubs steam_client.library_enumerate -> one app with version
    # (mirror the existing happy-path fixture; ensure the stubbed app dict includes
    #  "version": "42")
    await _steam_library_sync({"id": 1, "platform": "steam"}, steam_deps_with_app)
    row = await pool.read_one("SELECT current_version FROM games WHERE platform='steam'")
    assert row["current_version"] == "42"


async def test_steam_sync_preserves_cached_version_and_status(pool, steam_deps_with_app):
    # pre-seed a game row with cached_version + a lifecycle status; re-sync must not clobber them
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, status, cached_version, current_version)"
        " VALUES ('steam','10','Old',1,'up_to_date','99','99')"
    )
    await _steam_library_sync({"id": 1, "platform": "steam"}, steam_deps_with_app)  # app_id 10, version 42
    row = await pool.read_one("SELECT status, cached_version, current_version FROM games WHERE app_id='10'")
    assert row["status"] == "up_to_date"        # untouched
    assert row["cached_version"] == "99"        # untouched
    assert row["current_version"] == "42"       # updated
```

Add the import: `from orchestrator.jobs.handlers.library_sync import _steam_library_sync`.

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/jobs/test_library_sync_handler.py -k current_version -v`
Expected: FAIL — `current_version` is `None` (column not written) / KeyError on the stubbed app `"version"`.

- [ ] **Step 3: Implement**

Replace `_UPSERT_SQL` (lines 24-31):

```python
_UPSERT_SQL = (
    "INSERT INTO games (platform, app_id, title, owned, metadata, current_version) "
    "VALUES (?, ?, ?, 1, ?, ?) "
    "ON CONFLICT(platform, app_id) DO UPDATE SET "
    "  title = excluded.title, "
    "  owned = 1, "
    "  metadata = excluded.metadata, "
    "  current_version = excluded.current_version"
)
```

Update the module docstring line 7-9 to note `current_version` is now also updated (still preserves `status`, `cached_version`, and other lifecycle columns).

Steam call site (line 142) — read the version and pass it:

```python
        version = app.get("version")
        await deps.pool.execute_write(
            _UPSERT_SQL, ("steam", str(app_id_raw), title, metadata, version)
        )
```

Epic call site (line 72):

```python
        await deps.pool.execute_write(
            _UPSERT_SQL, ("epic", item.app_name, item.title, metadata, item.build_version)
        )
```

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/jobs/test_library_sync_handler.py -v`
Expected: PASS. (If the existing steam fixture builds apps without `"version"`, `app.get("version")` yields `None` — harmless; update the happy-path fixture to include `"version"` only where the new assertions need it.)

---

## Task 4: Prefill success writes `cached_version`

**Files:**
- Modify: `src/orchestrator/jobs/handlers/prefill.py` (line 315-317)
- Test: `tests/jobs/test_prefill_handler.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/jobs/test_prefill_handler.py  (add — mirror the existing fake_prefill happy path)
async def test_full_success_sets_cached_version_to_current(pool, prefill_deps, monkeypatch):
    # seed a game with current_version set, cached_version NULL
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, current_version, status)"
        " VALUES ('steam','10','G',1,'42','downloading')"
    )
    gid = (await pool.read_one("SELECT id FROM games WHERE app_id='10'"))["id"]
    # stub prefill_chunks -> all chunks ok (mirror existing happy-path stub)
    async def fake_prefill(uris, settings, *, on_progress=None):
        return PrefillResult(chunks_total=1, chunks_ok=1, chunks_failed=0, failures=[])
    monkeypatch.setattr("orchestrator.jobs.handlers.prefill.prefill_chunks", fake_prefill)
    monkeypatch.setattr("orchestrator.jobs.handlers.prefill._load_chunk_uris",
                        _async_return(["http://x/chunk/abc"]))
    await _steam_prefill_inner(1, gid, {"app_id": "10"}, prefill_deps, prefill_deps.steam_client)
    row = await pool.read_one("SELECT cached_version, last_prefilled_at FROM games WHERE id=?", (gid,))
    assert row["cached_version"] == "42"
    assert row["last_prefilled_at"] is not None


async def test_partial_failure_leaves_cached_version_stale(pool, prefill_deps, monkeypatch):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, current_version, cached_version)"
        " VALUES ('steam','11','G',1,'42','OLD')"
    )
    gid = (await pool.read_one("SELECT id FROM games WHERE app_id='11'"))["id"]
    async def fake_prefill(uris, settings, *, on_progress=None):
        return PrefillResult(chunks_total=2, chunks_ok=1, chunks_failed=1, failures=[{"status": 500}])
    monkeypatch.setattr("orchestrator.jobs.handlers.prefill.prefill_chunks", fake_prefill)
    monkeypatch.setattr("orchestrator.jobs.handlers.prefill._load_chunk_uris",
                        _async_return(["u1", "u2"]))
    with pytest.raises(RuntimeError):
        await _steam_prefill_inner(1, gid, {"app_id": "11"}, prefill_deps, prefill_deps.steam_client)
    row = await pool.read_one("SELECT cached_version FROM games WHERE id=?", (gid,))
    assert row["cached_version"] == "OLD"   # unchanged on failure
```

(Use the module's existing test helpers for stubbing `_load_chunk_uris` / `prefill_chunks`; `_async_return` is a tiny `async def` returning the value — define locally if not already present. Match the real `PrefillResult` constructor field names.)

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/jobs/test_prefill_handler.py -k cached_version -v`
Expected: FAIL — `cached_version` is `None` after success.

- [ ] **Step 3: Implement**

Replace the success-path `last_prefilled_at` write (lines 315-317):

```python
    await deps.pool.execute_write(
        "UPDATE games SET last_prefilled_at=CURRENT_TIMESTAMP, "
        "cached_version=current_version WHERE id=?",
        (game_id,),
    )
```

(The failure branch at lines 292-301 is unchanged — it sets `status='failed'` and raises before reaching this write, so `cached_version` stays stale.)

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/jobs/test_prefill_handler.py -v`
Expected: PASS.

---

## Task 5: Clean validation writes `cached_version`

**Files:**
- Modify: `src/orchestrator/jobs/handlers/validate.py` (`validate_one_game`, lines 66-79)
- Test: `tests/jobs/test_validate_handler.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/jobs/test_validate_handler.py  (add)
async def test_clean_validation_sets_cached_version(pool, validate_deps, monkeypatch):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, current_version, status)"
        " VALUES ('steam','10','G',1,'42','unknown')"
    )
    gid = (await pool.read_one("SELECT id FROM games WHERE app_id='10'"))["id"]
    _stub_validate_game(monkeypatch, outcome="cached", manifest_version="42",
                        total=3, cached=3, missing=0)
    await validate_one_game(pool, validate_deps, gid, get_settings())
    row = await pool.read_one("SELECT status, cached_version FROM games WHERE id=?", (gid,))
    assert row["status"] == "up_to_date"
    assert row["cached_version"] == "42"   # == current_version


async def test_failed_validation_leaves_cached_version_unchanged(pool, validate_deps, monkeypatch):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, current_version, cached_version, status)"
        " VALUES ('steam','11','G',1,'42','OLD','up_to_date')"
    )
    gid = (await pool.read_one("SELECT id FROM games WHERE app_id='11'"))["id"]
    _stub_validate_game(monkeypatch, outcome="missing", manifest_version="42",
                        total=3, cached=1, missing=2)
    await validate_one_game(pool, validate_deps, gid, get_settings())
    row = await pool.read_one("SELECT status, cached_version FROM games WHERE id=?", (gid,))
    assert row["status"] == "validation_failed"
    assert row["cached_version"] == "OLD"   # unchanged
```

(`_stub_validate_game` monkeypatches `orchestrator.jobs.handlers.validate.validate_game` to return a `ValidationResult` with the given fields — mirror the existing validate-handler test stubbing.)

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/jobs/test_validate_handler.py -k cached_version -v`
Expected: FAIL — `cached_version` is `None` after a clean validation.

- [ ] **Step 3: Implement**

Replace the status-update block (lines 66-79) so the `up_to_date` path also copies `current_version` into `cached_version`:

```python
    new_status = _STATUS_FOR.get(result.outcome)
    if new_status == "up_to_date":
        # Clean validation: what's on disk == the current version. Adopt it.
        await pool.execute_write(
            "UPDATE games SET status='up_to_date', last_validated_at=CURRENT_TIMESTAMP, "
            "cached_version=current_version WHERE id=?",
            (game_id,),
        )
    elif new_status is not None:  # 'validation_failed' — do NOT touch cached_version
        await pool.execute_write(
            "UPDATE games SET status=?, last_validated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, game_id),
        )
    else:
        # outcome='error' (infra failure). Never clobber a classified status, but
        # resolve the transient 'downloading' so a freshly-prefilled game isn't stuck.
        await pool.execute_write(
            "UPDATE games SET status='failed', last_error=? WHERE id=? AND status='downloading'",
            ((f"validate: {result.error}"[:200] if result.error else "validate: error"), game_id),
        )
```

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/jobs/test_validate_handler.py -v`
Expected: PASS.

---

## Task 6: Widen the sweep candidate query (cold-start adoption)

**Files:**
- Modify: `src/orchestrator/jobs/handlers/sweep.py` (`_CANDIDATE_SQL`, lines 24-28)
- Test: `tests/jobs/test_sweep_handler.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/test_sweep_handler.py  (add)
async def test_sweep_includes_unknown_and_not_downloaded(pool, sweep_deps, monkeypatch):
    for app_id, status in [("1", "unknown"), ("2", "not_downloaded"),
                           ("3", "up_to_date"), ("4", "validation_failed")]:
        await pool.execute_write(
            "INSERT INTO games (platform, app_id, title, owned, status)"
            " VALUES ('steam', ?, 'G', 1, ?)", (app_id, status)
        )
    seen: list[int] = []
    _stub_validate_one_game(monkeypatch, record_into=seen, outcome="cached")
    _stub_validator_healthy(monkeypatch, True)
    await sweep_handler({"id": 1}, sweep_deps)
    # all four candidate statuses validated (adoption pass covers unknown/not_downloaded)
    assert len(seen) == 4
```

(`_stub_validate_one_game` patches `orchestrator.jobs.handlers.sweep.validate_one_game` to append `game_id` to `record_into`; `_stub_validator_healthy` patches `validator_self_test` -> True. Mirror existing sweep tests.)

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/jobs/test_sweep_handler.py -k unknown_and_not_downloaded -v`
Expected: FAIL — only 2 validated (up_to_date + validation_failed).

- [ ] **Step 3: Implement**

Replace `_CANDIDATE_SQL` (lines 24-28):

```python
_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE platform='steam' "
    "AND status IN ('up_to_date','validation_failed','unknown','not_downloaded') "
    "ORDER BY id"
)
```

Update the module docstring (line 4) to note the candidate set now also covers never-validated games so the cold-start sweep adopts an existing cache.

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/jobs/test_sweep_handler.py -v`
Expected: PASS.

---

## Task 7: `enqueue_scheduled_prefill` (the version-diff insert)

**Files:**
- Modify: `src/orchestrator/scheduler/jobs.py` (append the new callback)
- Test: `tests/scheduler/test_jobs.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scheduler/test_jobs.py  (add)
from orchestrator.scheduler.jobs import enqueue_scheduled_prefill


async def _seed_game(pool, app_id, *, owned=1, current="42", cached=None, status="up_to_date", platform="steam"):
    await pool.execute_write(
        "INSERT INTO games (platform, app_id, title, owned, current_version, cached_version, status)"
        " VALUES (?, ?, 'G', ?, ?, ?, ?)",
        (platform, app_id, owned, current, cached, status),
    )


class TestEnqueueScheduledPrefill:
    async def test_enqueues_never_cached(self, pool):
        await _seed_game(pool, "1", current="42", cached=None)
        n = await enqueue_scheduled_prefill(pool)
        assert n == 1
        row = await pool.read_one("SELECT kind, platform, state, source FROM jobs LIMIT 1")
        assert (row["kind"], row["state"], row["source"]) == ("prefill", "queued", "scheduler")

    async def test_enqueues_when_version_diverged(self, pool):
        await _seed_game(pool, "1", current="42", cached="41")
        assert await enqueue_scheduled_prefill(pool) == 1

    async def test_enqueues_validation_failed(self, pool):
        await _seed_game(pool, "1", current="42", cached="42", status="validation_failed")
        assert await enqueue_scheduled_prefill(pool) == 1

    async def test_skips_up_to_date(self, pool):
        await _seed_game(pool, "1", current="42", cached="42", status="up_to_date")
        assert await enqueue_scheduled_prefill(pool) == 0

    async def test_skips_unowned(self, pool):
        await _seed_game(pool, "1", owned=0, current="42", cached=None)
        assert await enqueue_scheduled_prefill(pool) == 0

    async def test_skips_blocked(self, pool):
        await _seed_game(pool, "1", current="42", cached=None)
        await pool.execute_write(
            "INSERT INTO block_list (platform, app_id, source) VALUES ('steam','1','api')"
        )
        assert await enqueue_scheduled_prefill(pool) == 0

    async def test_dedups_inflight_prefill(self, pool):
        await _seed_game(pool, "1", current="42", cached=None)
        gid = (await pool.read_one("SELECT id FROM games LIMIT 1"))["id"]
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source)"
            " VALUES ('prefill', ?, 'steam', 'queued', 'api')", (gid,)
        )
        assert await enqueue_scheduled_prefill(pool) == 0  # ON CONFLICT DO NOTHING
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/scheduler/test_jobs.py -k ScheduledPrefill -v`
Expected: FAIL — `ImportError: cannot import name 'enqueue_scheduled_prefill'`.

- [ ] **Step 3: Implement** (append to `jobs.py`)

```python
async def enqueue_scheduled_prefill(pool: Pool) -> int:
    """Enqueue 'prefill' jobs for owned games that are new, version-diverged, or
    validation_failed — and not block-listed (F8 scheduled prefill driver).

    One bulk INSERT...SELECT. ON CONFLICT DO NOTHING + the migration-0006 in-flight
    UNIQUE index dedups against a prefill already queued/running for a game. Returns
    the number of rows enqueued (rowcount). Never raises — a failing scheduler tick
    must not degrade APScheduler.
    """
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "SELECT 'prefill', g.id, g.platform, 'queued', 'scheduler' "
            "FROM games g "
            "WHERE g.owned = 1 "
            "  AND (g.cached_version IS NULL "
            "       OR g.cached_version <> g.current_version "
            "       OR g.status = 'validation_failed') "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM block_list b "
            "      WHERE b.platform = g.platform AND b.app_id = g.app_id) "
            "ON CONFLICT DO NOTHING"
        )
        _log.info("scheduler.scheduled_prefill.enqueued", count=inserted)
        return inserted
    except PoolError as e:
        _log.error("scheduler.scheduled_prefill.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        _log.error(
            "scheduler.scheduled_prefill.unexpected_error",
            error=type(e).__name__,
            reason=str(e)[:200],
        )
        return 0
```

**Note on the diff:** a game with `current_version IS NULL` (never version-resolved) is caught by the `cached_version IS NULL` clause and enqueued, which resolves it. `cached_version <> current_version` is NULL-safe because the `cached_version IS NULL` disjunct already covers the NULL-cached case.

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/scheduler/test_jobs.py -v`
Expected: PASS.

---

## Task 8: Register the scheduled-prefill job

**Files:**
- Modify: `src/orchestrator/core/settings.py` (add `scheduled_prefill_enabled: bool = True`)
- Modify: `src/orchestrator/scheduler/manager.py` (job id, constructor param, registration)
- Modify: the lifespan/factory that constructs `SchedulerManager` (pass `scheduled_prefill_enabled` from settings — find via `grep -rn "SchedulerManager(" src/orchestrator`)
- Test: `tests/scheduler/test_manager.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scheduler/test_manager.py  (add)
async def test_registers_scheduled_prefill_job(pool):
    from orchestrator.scheduler.manager import SCHEDULED_PREFILL_JOB_ID, SchedulerManager
    mgr = SchedulerManager(
        pool=pool, enabled=True, library_sync_interval_sec=21600,
        validation_sweep_enabled=False, scheduled_prefill_enabled=True,
    )
    await mgr.start()
    try:
        assert SCHEDULED_PREFILL_JOB_ID in mgr.get_registered_job_ids()
    finally:
        await mgr.shutdown()


async def test_scheduled_prefill_disabled_not_registered(pool):
    from orchestrator.scheduler.manager import SCHEDULED_PREFILL_JOB_ID, SchedulerManager
    mgr = SchedulerManager(
        pool=pool, enabled=True, library_sync_interval_sec=21600,
        validation_sweep_enabled=False, scheduled_prefill_enabled=False,
    )
    await mgr.start()
    try:
        assert SCHEDULED_PREFILL_JOB_ID not in mgr.get_registered_job_ids()
    finally:
        await mgr.shutdown()
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/scheduler/test_manager.py -k scheduled_prefill -v`
Expected: FAIL — `ImportError`/`TypeError` (no such constant / unexpected kwarg).

- [ ] **Step 3: Implement**

`settings.py`: add the field next to the other scheduler settings:

```python
    scheduled_prefill_enabled: bool = True
```

`manager.py`:
- Add the import: `from orchestrator.scheduler.jobs import (enqueue_library_sync, enqueue_scheduled_prefill, enqueue_validation_sweep)`.
- Add the constant (after line 39): `SCHEDULED_PREFILL_JOB_ID = "scheduled_prefill"`.
- Add constructor param + field:

```python
        validation_sweep_cron: str = "0 3 * * 0",
        scheduled_prefill_enabled: bool = True,
    ) -> None:
        ...
        self._scheduled_prefill_enabled = scheduled_prefill_enabled
```

- Register the job inside `start()`, after the validation-sweep block (before `scheduler.start()`):

```python
            if self._scheduled_prefill_enabled:
                scheduler.add_job(
                    enqueue_scheduled_prefill,
                    trigger=IntervalTrigger(seconds=self._library_sync_interval_sec),
                    args=(self._pool,),
                    id=SCHEDULED_PREFILL_JOB_ID,
                    name="Enqueue scheduled prefill (version-diff)",
                    replace_existing=True,
                )
```

Update the construction site (from the grep) to pass `scheduled_prefill_enabled=settings.scheduled_prefill_enabled`.

**Scheduling note:** the prefill job runs on the same 6h interval as `library_sync`, independent of it (no completion-chaining). This is eventually-consistent: a freshly released patch is enqueued once both the next `library_sync` (refreshing `current_version`) and the next prefill diff have run — worst-case ~2 cycles. Acceptable for a 6h cadence, and avoids coupling the worker handler to scheduling.

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/scheduler/test_manager.py -v`
Expected: PASS.

---

## Task 9: Block-list REST router

**Files:**
- Create: `src/orchestrator/api/routers/block_list.py`
- Modify: `src/orchestrator/api/main.py` (import + `include_router`, after line 397)
- Test: `tests/api/test_block_list_router.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_block_list_router.py  (new)
from __future__ import annotations

VALID_TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


class TestBlockListPost:
    async def test_post_creates_returns_201(self, client, populated_pool):
        r = await client.post("/api/v1/block-list",
                              json={"platform": "steam", "app_id": "730", "reason": "no"}, headers=AUTH)
        assert r.status_code == 201
        body = r.json()
        assert body["platform"] == "steam" and body["app_id"] == "730"
        assert body["reason"] == "no" and body["source"] == "api"
        assert set(body) == {"id", "platform", "app_id", "reason", "source", "blocked_at"}

    async def test_post_idempotent_returns_200(self, client, populated_pool):
        await client.post("/api/v1/block-list", json={"platform": "steam", "app_id": "730"}, headers=AUTH)
        r = await client.post("/api/v1/block-list", json={"platform": "steam", "app_id": "730"}, headers=AUTH)
        assert r.status_code == 200

    async def test_post_accepts_unknown_app_id_preblock(self, client, populated_pool):
        r = await client.post("/api/v1/block-list",
                              json={"platform": "epic", "app_id": "never-seen"}, headers=AUTH)
        assert r.status_code == 201

    async def test_post_rejects_extra_field_422(self, client, populated_pool):
        r = await client.post("/api/v1/block-list",
                              json={"platform": "steam", "app_id": "1", "nope": 1}, headers=AUTH)
        assert r.status_code == 422

    async def test_post_rejects_bad_platform_422(self, client, populated_pool):
        r = await client.post("/api/v1/block-list", json={"platform": "gog", "app_id": "1"}, headers=AUTH)
        assert r.status_code == 422

    async def test_post_requires_auth_401(self, client, populated_pool):
        r = await client.post("/api/v1/block-list", json={"platform": "steam", "app_id": "1"})
        assert r.status_code == 401


class TestBlockListGet:
    async def test_get_empty_envelope(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM block_list")
        r = await client.get("/api/v1/block-list", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["block_list"] == [] and body["meta"]["total"] == 0

    async def test_get_filter_by_platform(self, client, populated_pool):
        for p, a in [("steam", "1"), ("epic", "2")]:
            await client.post("/api/v1/block-list", json={"platform": p, "app_id": a}, headers=AUTH)
        r = await client.get("/api/v1/block-list?platform=steam", headers=AUTH)
        rows = r.json()["block_list"]
        assert [x["platform"] for x in rows] == ["steam"]

    async def test_get_rejects_unknown_filter_400(self, client, populated_pool):
        r = await client.get("/api/v1/block-list?bogus=1", headers=AUTH)
        assert r.status_code == 400


class TestBlockListDelete:
    async def test_delete_present_removes_1(self, client, populated_pool):
        await client.post("/api/v1/block-list", json={"platform": "steam", "app_id": "9"}, headers=AUTH)
        r = await client.delete("/api/v1/block-list/steam/9", headers=AUTH)
        assert r.status_code == 200 and r.json() == {"removed": 1}

    async def test_delete_absent_idempotent_removes_0(self, client, populated_pool):
        r = await client.delete("/api/v1/block-list/steam/absent", headers=AUTH)
        assert r.status_code == 200 and r.json() == {"removed": 0}
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/api/test_block_list_router.py -v`
Expected: FAIL — 404 on all routes (router not registered).

- [ ] **Step 3: Implement** (`src/orchestrator/api/routers/block_list.py`)

```python
"""Block-list REST resource (F8). GET (paginated) / POST (idempotent) / DELETE.

block_list is the single source of truth for "skip during scheduled prefill".
POST accepts an unknown (platform, app_id) so an app can be pre-blocked before
the orchestrator has enumerated it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api._query_helpers import (
    FilterAllowList,
    FilterFieldSpec,
    QueryParamError,
    SortAllowList,
    SortField as _SortField,
    SortFieldResponse,
    build_order_by_clause,
    build_where_clause,
    parse_filters,
    parse_pagination,
    parse_sort,
)
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["block_list"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
DEFAULT_SORT = (_SortField(field="blocked_at", direction="desc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

BLOCK_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "platform": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "source": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
    }
)
BLOCK_SORT_ALLOW_LIST = SortAllowList(fields={"blocked_at", "platform", "app_id", "id"})

_COLUMNS = "id, platform, app_id, reason, source, blocked_at"


class BlockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    platform: Literal["steam", "epic"]
    app_id: str
    reason: str | None
    source: Literal["cli", "gameshelf", "api", "config"]
    blocked_at: str


class BlockListMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, dict[str, object]]
    applied_sort: list[SortFieldResponse]


class BlockListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_list: list[BlockEntry]
    meta: BlockListMeta


class BlockCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["steam", "epic"]
    app_id: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=500)
    source: Literal["cli", "gameshelf", "api", "config"] = "api"


def _row_to_entry(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": int(row["id"]),  # type: ignore[arg-type]
        "platform": row["platform"],
        "app_id": row["app_id"],
        "reason": row["reason"],
        "source": row["source"],
        "blocked_at": row["blocked_at"],
    }


@router.get("/block-list", response_model=BlockListResponse)
async def list_block_list(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        pagination = parse_pagination(request.query_params, default_limit=DEFAULT_LIMIT, max_limit=MAX_LIMIT)
        filters = parse_filters(request.query_params, allow_list=BLOCK_FILTER_ALLOW_LIST)
        sort = parse_sort(request.query_params, allow_list=BLOCK_SORT_ALLOW_LIST,
                          default=list(DEFAULT_SORT), tie_breaker=TIE_BREAKER)
    except QueryParamError as e:
        return JSONResponse(content={"detail": str(e)}, status_code=400)

    where_sql, where_params = build_where_clause(filters, allow_list=BLOCK_FILTER_ALLOW_LIST)
    order_sql = build_order_by_clause(sort, allow_list=BLOCK_SORT_ALLOW_LIST)
    # nosem: S608 — where_sql/order_sql are allow-list-validated field names only;
    # values flow through `?` placeholders (see games.py + _query_helpers invariants).
    count_sql = f"SELECT COUNT(*) AS total FROM block_list {where_sql}".strip()  # noqa: S608
    rows_sql = (f"SELECT {_COLUMNS} FROM block_list {where_sql} {order_sql} LIMIT ? OFFSET ?").strip()  # noqa: S608
    rows_params = [*where_params, pagination.limit, pagination.offset]

    try:
        count_row = await pool.read_one(count_sql, where_params)
        rows = await pool.read_all(rows_sql, rows_params)
    except PoolError as e:
        _log.error("api.block_list.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)

    total = int(count_row["total"]) if count_row else 0
    entries = [_row_to_entry(r) for r in rows]
    body = BlockListResponse(
        block_list=[BlockEntry(**e) for e in entries],  # type: ignore[arg-type]
        meta=BlockListMeta(
            total=total, limit=pagination.limit, offset=pagination.offset,
            has_more=(pagination.offset + len(entries) < total),
            applied_filters={f: dict(ops) for f, ops in filters.items()},
            applied_sort=[SortFieldResponse(field=s.field, direction=s.direction) for s in sort],
        ),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))


@router.post("/block-list", response_model=BlockEntry,
             responses={200: {"description": "Already blocked"}, 201: {"description": "Blocked"}})
async def create_block(
    payload: BlockCreate,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        inserted = await pool.execute_write(
            "INSERT INTO block_list (platform, app_id, reason, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(platform, app_id) DO NOTHING",
            (payload.platform, payload.app_id, payload.reason, payload.source),
        )
        row = await pool.read_one(
            f"SELECT {_COLUMNS} FROM block_list WHERE platform=? AND app_id=?",
            (payload.platform, payload.app_id),
        )
    except PoolError as e:
        _log.error("api.block_list.write_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    if row is None:  # pragma: no cover - write succeeded but row vanished
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    entry = BlockEntry(**_row_to_entry(row))  # type: ignore[arg-type]
    return JSONResponse(content=entry.model_dump(), status_code=201 if inserted else 200)


@router.delete("/block-list/{platform}/{app_id}")
async def delete_block(
    platform: str,
    app_id: str,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    try:
        removed = await pool.execute_write(
            "DELETE FROM block_list WHERE platform=? AND app_id=?", (platform, app_id)
        )
    except PoolError as e:
        _log.error("api.block_list.delete_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)
    return JSONResponse(content={"removed": int(removed)}, status_code=200)
```

In `main.py`, add the import alongside the other routers and register after `games_router` (line 397):

```python
    from orchestrator.api.routers.block_list import router as block_list_router
    # ...
    app.include_router(block_list_router)
```

(Match the existing import style — the other routers are imported at the top of `main.py`; add `block_list` there and include it in the same block as the others.)

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/api/test_block_list_router.py -v`
Expected: PASS. Confirm `parse_pagination`/`parse_filters`/`parse_sort`/`build_*` import names match `_query_helpers.py` (they are the same names `games.py` imports).

---

## Task 10: `blocked` field on GameResponse

**Files:**
- Modify: `src/orchestrator/api/routers/games.py` (`GameResponse` ~line 86, `rows_sql` ~line 190, row mapping ~line 254)
- Test: `tests/api/test_games_router.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_games_router.py  (add)
class TestGamesBlockedField:
    async def test_blocked_true_when_in_block_list(self, client, populated_pool):
        g = (await client.get("/api/v1/games", headers={"Authorization": f"Bearer {'a'*32}"})).json()["games"][0]
        await populated_pool.execute_write(
            "INSERT INTO block_list (platform, app_id, source) VALUES (?, ?, 'api')",
            (g["platform"], g["app_id"]),
        )
        body = (await client.get("/api/v1/games", headers={"Authorization": f"Bearer {'a'*32}"})).json()
        match = next(x for x in body["games"] if x["id"] == g["id"])
        assert match["blocked"] is True
        others = [x for x in body["games"] if x["id"] != g["id"]]
        assert all(x["blocked"] is False for x in others)
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/api/test_games_router.py -k blocked -v`
Expected: FAIL — `KeyError: 'blocked'` / response model has no such field.

- [ ] **Step 3: Implement**

Add `blocked: bool` to `GameResponse` (after `metadata`, line 110):

```python
    metadata: dict[str, Any] | None
    blocked: bool
```

Change `rows_sql` (line 190-192) to add a correlated `EXISTS` subquery — no JOIN, so the allow-list `where_sql`/`order_sql` (bare `games` column names) stay unambiguous:

```python
    rows_sql = (
        f"SELECT {_GAMES_COLUMNS}, "
        "EXISTS(SELECT 1 FROM block_list b "
        "WHERE b.platform=games.platform AND b.app_id=games.app_id) AS blocked "
        f"FROM games {where_sql} {order_sql} LIMIT ? OFFSET ?"  # noqa: S608
    ).strip()
```

In the `GameResponse(...)` construction (after `metadata=metadata,`, line 268), add:

```python
                    metadata=metadata,
                    blocked=bool(row["blocked"]),
```

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/api/test_games_router.py -v`
Expected: PASS (the existing games tests still pass — `blocked` is additive).

---

## Task 11: CLI `game block` / `game unblock` + `blocked` column

**Files:**
- Modify: `src/orchestrator/cli/client.py` (add `delete`)
- Modify: `src/orchestrator/cli/commands/game.py` (block/unblock commands + list column)
- Test: `tests/cli/test_cmd_game.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_cmd_game.py  (add — mirror the existing `mock` fixture pattern)
import httpx


def test_game_block_resolves_and_posts(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games":
            return httpx.Response(200, json={"games": [
                {"id": 5, "platform": "steam", "app_id": "730", "title": "CS", "status": "up_to_date", "blocked": False}
            ], "meta": {}})
        assert req.method == "POST" and req.url.path == "/api/v1/block-list"
        import json as _j
        body = _j.loads(req.content)
        assert body == {"platform": "steam", "app_id": "730", "reason": "x", "source": "cli"}
        return httpx.Response(201, json={"id": 1, "platform": "steam", "app_id": "730",
                                         "reason": "x", "source": "cli", "blocked_at": "t"})
    r = mock(["game", "block", "5", "--reason", "x"], handler)
    assert r.exit_code == 0 and "730" in r.output


def test_game_block_unknown_id_exit_1(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"games": [], "meta": {}})
    r = mock(["game", "block", "999"], handler)
    assert r.exit_code == 1


def test_game_unblock_resolves_and_deletes(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games":
            return httpx.Response(200, json={"games": [
                {"id": 5, "platform": "steam", "app_id": "730", "title": "CS", "status": "up_to_date", "blocked": True}
            ], "meta": {}})
        assert req.method == "DELETE" and req.url.path == "/api/v1/block-list/steam/730"
        return httpx.Response(200, json={"removed": 1})
    r = mock(["game", "unblock", "5"], handler)
    assert r.exit_code == 0


def test_game_list_shows_blocked_column(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"games": [
            {"id": 5, "platform": "steam", "app_id": "730", "title": "CS", "status": "up_to_date", "blocked": True}
        ], "meta": {}})
    r = mock(["game", "list"], handler)
    assert r.exit_code == 0 and "BLOCKED" in r.output
```

- [ ] **Step 2: Run to verify red**

Run: `.venv/bin/pytest tests/cli/test_cmd_game.py -k "block or blocked_column" -v`
Expected: FAIL — no `block`/`unblock` command; `BLOCKED` not in output; `OrchClient` has no `delete`.

- [ ] **Step 3: Implement**

`client.py` — add after `post` (line 104):

```python
    def delete(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return self._request("DELETE", path, json=json)
```

`game.py` — add a resolve helper + two commands, and the list column. Add helper near `_trigger`:

```python
def _resolve_app(ctx: click.Context, game_id: int) -> tuple[object, tuple[str, str]]:
    """Return (client, (platform, app_id)) for a known game id, or raise ApiError."""
    client = make_client(ctx)
    data = client.get("/api/v1/games", limit=500)
    match = next((g for g in data["games"] if g["id"] == game_id), None)
    if match is None:
        raise ApiError(f"game {game_id} not found (in the first 500)")
    return client, (match["platform"], match["app_id"])


@game.command("block")
@click.argument("game_id", type=int, callback=_positive_int)
@click.option("--reason", default=None, help="Optional note (<=500 chars).")
@click.pass_context
@handles_api_errors
def game_block(ctx: click.Context, game_id: int, reason: str | None) -> None:
    """Exclude a game from scheduled prefill."""
    client, (platform, app_id) = _resolve_app(ctx, game_id)
    client.post("/api/v1/block-list",
                json={"platform": platform, "app_id": app_id, "reason": reason, "source": "cli"})
    output.success(f"blocked game {game_id} ({platform}:{app_id}) from scheduled prefill.")


@game.command("unblock")
@click.argument("game_id", type=int, callback=_positive_int)
@click.pass_context
@handles_api_errors
def game_unblock(ctx: click.Context, game_id: int) -> None:
    """Remove a game from the block list (idempotent)."""
    client, (platform, app_id) = _resolve_app(ctx, game_id)
    resp = client.delete(f"/api/v1/block-list/{platform}/{app_id}")
    removed = (resp or {}).get("removed", 0)
    if removed:
        output.success(f"unblocked game {game_id} ({platform}:{app_id}).")
    else:
        output.success(f"game {game_id} ({platform}:{app_id}) was not blocked.")
```

In `game_list` (line 49-59), add the blocked column:

```python
    rows = [
        [
            str(g["id"]),
            g["platform"],
            g["app_id"],
            (g.get("title") or "")[:40],
            output.status_label(g["status"]),
            "yes" if g.get("blocked") else "-",
        ]
        for g in data["games"]
    ]
    click.echo(output.table(["ID", "PLATFORM", "APP_ID", "TITLE", "STATUS", "BLOCKED"], rows))
```

(Remove the `reason` mypy concern: `json=` dict values are `str | None` — `OrchClient.post`/`delete` accept `dict[str, Any] | None`, fine.)

- [ ] **Step 4: Run to verify green**

Run: `.venv/bin/pytest tests/cli/test_cmd_game.py -v`
Expected: PASS.

---

## Task 12: Full-suite + adjacent regression check

- [ ] **Step 1:** Run the whole suite: `.venv/bin/pytest -q`
Expected: all pass. Pay attention to existing library_sync / games-router / scheduler tests that may assert exact column sets or job counts.
- [ ] **Step 2:** Fix any regressions revealed (e.g., a library_sync happy-path fixture that now needs a `"version"` key; a games-router snapshot asserting the field set — add `blocked`).
- [ ] **Step 3:** `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests`
- [ ] **Step 4:** `.venv/bin/mypy --strict src/` — resolve any typing gaps (annotate new functions fully; `BlockCreate`/`BlockEntry` are typed; the `_resolve_app` return tuple is annotated).

---

## Task 13: Gate sweep, audit, docs, commit, PR

- [ ] **Step 1: Process checklist — start the feature**
`bash scripts/process-checklist.sh --start-feature "f8-block-list"`

- [ ] **Step 2: Full gate sweep**
```
.venv/bin/pytest -q
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy --strict src/
gitleaks detect --no-banner
semgrep --config .semgrep.yml --error   # or the repo's configured semgrep invocation
```
Expected: all green. Mark: `bash scripts/process-checklist.sh --complete-step build_loop:tests_written` … through `build_loop:implemented` per the actual step ids (tests_written, tests_verified_failing, implemented).

- [ ] **Step 3: Security audit doc**
Write `docs/security-audits/f8-block-list-security-audit.md` covering: SQL-injection surface (block-list filters/sort go through the same allow-list + `?` placeholders as games — no raw interpolation of values; `DELETE`/`POST` use bound params), auth (all block-list endpoints behind bearer middleware; verified by `test_post_requires_auth_401`), input bounds (`app_id` ≤64, `reason` ≤500, `platform`/`source` Literal — enforced by pydantic AND the table CHECK constraints), idempotency/DoS (ON CONFLICT — a flood of duplicate blocks is O(1) no-ops), and the scheduled-prefill enqueue (bounded by library size, deduped by the in-flight index). Mark `build_loop:security_audit`.

- [ ] **Step 4: Adversarial-verify Workflow** over the batch (every prior batch caught a real defect). Dispatch a single adversarial reviewer (Agent tool, code-reviewer or general) to attack: NULL-safety of the diff `<>`, column-ambiguity in the games EXISTS subquery, the 201-vs-200 rowcount logic, block-list bypass of manual triggers (should still work), and the `cached_version` write only-on-success invariant. Fix anything real test-first.

- [ ] **Step 5: Docs**
- `CHANGELOG.md` (8 categories): **Added** — block-list REST API (GET/POST/DELETE) + CLI `game block/unblock` + `games.blocked`; scheduled prefill driver (version-diff). **Changed** — `library_sync` now writes `current_version`; prefill/validate write `cached_version`; sweep candidate set widened for cold-start adoption. **Data Model** — `games.current_version`/`cached_version` now populated (no schema change); `block_list` now consumed. **Security** — block-list endpoints bearer-gated + bound-checked.
- `FEATURES.md`: new "Feature N: F8 — Block List + Scheduled Prefill Driver" record (status Complete pending live UAT), key interfaces, and the cold-start adoption note.
- Mark `build_loop:documentation_updated`.

- [ ] **Step 6: Feature record**
`bash scripts/test-gate.sh --record-feature "f8-block-list"` and `bash scripts/process-checklist.sh --complete-step build_loop:feature_recorded`.

- [ ] **Step 7: Evaluate gate** — present the implementation evaluation (what was built, risks, the eventual-consistency scheduling choice, cold-start procedure) to the Orchestrator; on approval run `bash .claude/framework/hooks/mark-evaluated.sh "..."`.

- [ ] **Step 8: Commit** — bring the A/B/C commit structure to the Orchestrator FIRST (per the commit-approval protocol), then a single `feat(f8): block-list + scheduled prefill driver (version-diff)` commit. `gitleaks protect --staged` before committing.

- [ ] **Step 9: PR** — open with `gh pr create`. Do NOT merge — the Orchestrator merges on GitHub.

---

## Cold-start adoption (operational, post-merge)

After deploy, trigger one validation sweep so the existing 12 TB cache is adopted (stat-only, no re-download): the widened sweep validates every owned Steam game; cached ones get `cached_version=current_version` and the scheduled diff then skips them. Document this in the PR body and FEATURES record. Live verification (full driver cycle) needs a Steam session (2FA) — flag as the manual UAT step.
