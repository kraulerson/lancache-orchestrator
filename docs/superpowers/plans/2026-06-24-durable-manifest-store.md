# Durable Steam Manifest Store + Validate-All Backfill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the orchestrator validate the *entire* prefilled Steam library by copying SteamPrefill's transient `.bin` manifests into a permanent agent-owned archive (validate reads live∪archive), plus a one-off "validate-all" full sweep to flip genuinely-cached games to `up_to_date`.

**Architecture:** Agent half — a new append-only manifest archive volume, a `union` read across [live cache, archive], and a periodic sync that copies new manifests into the archive before SteamPrefill prunes them. Control-plane half — the F13 sweep gains a `full` mode (carried on `jobs.payload`, no migration) that validates every steam game, triggered by a new `POST /api/v1/sweep` endpoint and an `orchestrator-cli cache validate-all` command.

**Tech Stack:** Python 3.12, FastAPI, pydantic-settings, click, structlog, stdlib `shutil`/`asyncio`; pytest/ruff/mypy.

**Spec:** `docs/superpowers/specs/2026-06-24-durable-manifest-store-design.md`

**Conventions:** TDD (failing test → red → minimal impl → green). **No per-task commits — ONE `feat` commit at the very end** (Task 8). Before that commit: `.venv/bin/ruff format`, `.venv/bin/ruff check`, `.venv/bin/mypy src/orchestrator` (all clean), full suite `.venv/bin/python -m pytest -q --ignore=tests/scripts` (only acceptable failure: `tests/test_licenses.py`). No `assert` in `src/` (ruff S101 → use `if … raise`); bare dict returns need `dict[str, Any]`. No new third-party libs (stdlib + already-researched fastapi/pydantic/click). Mark each task `in_progress` (enforce-plan-tracking) before editing its source.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `src/orchestrator/core/settings.py` | new `steam_manifest_archive_dir`, `manifest_archive_sync_interval_sec` | modify |
| `src/orchestrator/agent/manifest_locator.py` | union read across multiple cache roots | modify (signature) |
| `src/orchestrator/agent/routers/steam.py` | pass `cache_roots=[live, archive]` | modify (2 call sites) |
| `src/orchestrator/agent/manifest_archive.py` | append-only sync + periodic loop (stdlib only) | **create** |
| `src/orchestrator/agent/app.py` | wire the periodic sync task into the lifespan | modify |
| `src/orchestrator/jobs/handlers/sweep.py` | `full` mode candidate SQL via `payload` | modify |
| `src/orchestrator/scheduler/jobs.py` | `enqueue_validation_sweep(*, full, source)` payload | modify |
| `src/orchestrator/api/routers/sweep_trigger.py` | `POST /api/v1/sweep` | **create** |
| `src/orchestrator/api/main.py` | register the sweep-trigger router | modify |
| `src/orchestrator/cli/commands/cache.py` | `cache validate-all` command | **create** |
| `src/orchestrator/cli/main.py` | register the `cache` group | modify |

---

## Task 1: Settings — archive dir + sync interval

**Files:**
- Modify: `src/orchestrator/core/settings.py` (after `steam_manifest_cache_dir`, ~line 109; `Field` import already present)
- Test: `tests/core/test_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_settings.py`:

```python
def test_manifest_archive_defaults():
    s = Settings()
    assert str(s.steam_manifest_archive_dir) == "/manifest-archive"
    assert s.manifest_archive_sync_interval_sec == 1800


def test_manifest_archive_env_override(monkeypatch):
    monkeypatch.setenv("ORCH_STEAM_MANIFEST_ARCHIVE_DIR", "/tmp/arch")
    monkeypatch.setenv("ORCH_MANIFEST_ARCHIVE_SYNC_INTERVAL_SEC", "0")
    s = Settings()
    assert str(s.steam_manifest_archive_dir) == "/tmp/arch"
    assert s.manifest_archive_sync_interval_sec == 0
```

(If `tests/core/test_settings.py` imports `Settings` differently, match the file's existing import. Env prefix is `ORCH_`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/core/test_settings.py -k manifest_archive -q`
Expected: FAIL (`AttributeError: 'Settings' object has no attribute 'steam_manifest_archive_dir'`).

- [ ] **Step 3: Implement**

In `src/orchestrator/core/settings.py`, immediately after the `steam_manifest_cache_dir` field:

```python
    # Durable manifest archive (2026-06-24). SteamPrefill only writes a manifest
    # .bin when an app has NEW content, so its live cache covers a shrinking
    # subset of the prefilled library. The agent copies every manifest it sees
    # into this permanent, append-only archive (immune to SteamPrefill
    # `clear-temp`); validate reads the UNION of the live cache + this archive.
    # An absent/unmounted dir is a no-op — byte-identical to pre-archive behavior.
    steam_manifest_archive_dir: Path = Path("/manifest-archive")
    # Agent sync cadence (seconds) for copying live manifests into the archive.
    # 0 disables the periodic sync.
    manifest_archive_sync_interval_sec: int = Field(default=1800, ge=0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/core/test_settings.py -k manifest_archive -q`
Expected: PASS.

---

## Task 2: manifest_locator — union across cache roots

**Files:**
- Modify: `src/orchestrator/agent/manifest_locator.py`
- Modify: `src/orchestrator/agent/routers/steam.py` (lines ~82-87 `prefilled_apps`, ~112-114 `steam_validate`)
- Test: `tests/agent/test_manifest_locator.py` (update existing calls + add new)

- [ ] **Step 1: Write the failing tests**

Replace the body of `tests/agent/test_manifest_locator.py` calls so every `cache_root=X` becomes `cache_roots=[X]`, and add:

```python
def _write_bin(root: Path, app: int, depot: int, gid: int, mtime: float | None = None) -> Path:
    v1 = root / "v1"
    v1.mkdir(parents=True, exist_ok=True)
    p = v1 / f"{app}_{app}_{depot}_{gid}.bin"
    p.write_bytes(b"x")
    if mtime is not None:
        import os
        os.utime(p, (mtime, mtime))
    return p


def test_union_live_only(tmp_path):
    live = tmp_path / "live"
    _write_bin(live, 440, 441, 111)
    assert locate_manifest_bins(440, cache_roots=[live, tmp_path / "absent"])


def test_union_archive_only(tmp_path):
    arch = tmp_path / "arch"
    _write_bin(arch, 730, 731, 222)
    found = locate_manifest_bins(730, cache_roots=[tmp_path / "absent", arch])
    assert len(found) == 1


def test_union_newest_per_depot_across_roots(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _write_bin(arch, 570, 571, 1, mtime=1000.0)   # older, archived
    newer = _write_bin(live, 570, 571, 2, mtime=2000.0)  # newer, live, same depot
    found = locate_manifest_bins(570, cache_roots=[live, arch])
    assert found == [newer]  # newest-per-depot wins regardless of root order


def test_union_both_absent_returns_empty(tmp_path):
    assert locate_manifest_bins(1, cache_roots=[tmp_path / "a", tmp_path / "b"]) == []


def test_list_prefilled_app_ids_union(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _write_bin(live, 440, 441, 1)
    _write_bin(arch, 730, 731, 1)
    assert list_prefilled_app_ids(cache_roots=[live, arch]) == [440, 730]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/agent/test_manifest_locator.py -q`
Expected: FAIL (`TypeError: … unexpected keyword argument 'cache_roots'`).

- [ ] **Step 3: Implement the union**

Replace both functions in `src/orchestrator/agent/manifest_locator.py` (keep the module docstring; update the param wording):

```python
def list_prefilled_app_ids(*, cache_roots: list[Path]) -> list[int]:
    """Distinct app_ids with a cached manifest .bin across all roots (sorted)."""
    apps: set[int] = set()
    for root in cache_roots:
        v1 = root / "v1"
        if not v1.is_dir():
            continue
        for path in v1.glob("*.bin"):
            first = path.stem.split("_", 1)[0]
            if first.isdigit():
                apps.add(int(first))
    return sorted(apps)


def locate_manifest_bins(app_id: int, *, cache_roots: list[Path]) -> list[Path]:
    """Newest manifest .bin per depot for ``app_id`` across all roots (empty if none).

    Roots are searched in order; the newest .bin per depot by mtime wins, so a
    fresher live-cache manifest supersedes an older archived one for the same
    depot (and stable apps present only in the archive are still found)."""
    newest_per_depot: dict[str, Path] = {}
    for root in cache_roots:
        v1 = root / "v1"
        if not v1.is_dir():
            continue
        for path in v1.glob(f"{app_id}_{app_id}_*.bin"):
            parts = path.stem.split("_")
            if len(parts) != 4:
                continue
            depot = parts[2]
            current = newest_per_depot.get(depot)
            if current is None or path.stat().st_mtime > current.stat().st_mtime:
                newest_per_depot[depot] = path
    return list(newest_per_depot.values())
```

- [ ] **Step 4: Update the two callers in `src/orchestrator/agent/routers/steam.py`**

`prefilled_apps` (replace the body lines that build `manifest_cache` + return):

```python
    s = request.app.state.settings
    roots = [Path(s.steam_manifest_cache_dir), Path(s.steam_manifest_archive_dir)]
    return {"app_ids": list_prefilled_app_ids(cache_roots=roots)}
```

`steam_validate` (replace the `manifest_cache = …` + `bins = locate_manifest_bins(…)` lines):

```python
    roots = [Path(settings.steam_manifest_cache_dir), Path(settings.steam_manifest_archive_dir)]
    bins = locate_manifest_bins(body.app_id, cache_roots=roots)
```

(`Path` is already imported in `steam.py`. The `slice_range`/`identifier`/`levels` lines below are unchanged.)

- [ ] **Step 5: Run to verify pass (incl. existing steam-validate tests)**

Run: `.venv/bin/python -m pytest tests/agent/test_manifest_locator.py tests/agent/test_steam_validate.py tests/agent/test_steam.py -q`
Expected: PASS. (If `test_steam_validate.py`/`test_steam.py` set up a manifest dir, they still work because the absent archive root is a no-op.)

---

## Task 3: manifest_archive.py — append-only sync (stdlib only)

**Files:**
- Create: `src/orchestrator/agent/manifest_archive.py`
- Test: `tests/agent/test_manifest_archive.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/agent/test_manifest_archive.py`:

```python
import os
import time
from pathlib import Path

import orchestrator.agent.manifest_archive as mod
from orchestrator.agent.manifest_archive import sync_manifests_to_archive


def _bin(root: Path, name: str, age_seconds: float = 100.0) -> Path:
    v1 = root / "v1"
    v1.mkdir(parents=True, exist_ok=True)
    p = v1 / name
    p.write_bytes(b"data")
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


def test_copies_new_bin(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _bin(live, "440_440_441_1.bin")
    assert sync_manifests_to_archive(live, arch) == 1
    assert (arch / "v1" / "440_440_441_1.bin").is_file()


def test_skips_already_archived(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _bin(live, "440_440_441_1.bin")
    _bin(arch, "440_440_441_1.bin")
    assert sync_manifests_to_archive(live, arch) == 0


def test_preserves_mtime(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    src = _bin(live, "1_1_2_3.bin", age_seconds=5000.0)
    sync_manifests_to_archive(live, arch)
    assert (arch / "v1" / "1_1_2_3.bin").stat().st_mtime == src.stat().st_mtime


def test_settle_guard_skips_too_fresh(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _bin(live, "9_9_9_9.bin", age_seconds=0.0)  # written "now"
    assert sync_manifests_to_archive(live, arch, settle_seconds=10.0) == 0


def test_tolerates_unreadable_file(tmp_path, monkeypatch):
    live, arch = tmp_path / "live", tmp_path / "arch"
    _bin(live, "1_1_2_3.bin")
    _bin(live, "4_4_5_6.bin")
    real = mod.shutil.copy2
    def flaky(src, dst, *a, **k):
        if Path(src).name == "1_1_2_3.bin":
            raise OSError("boom")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(mod.shutil, "copy2", flaky)
    assert sync_manifests_to_archive(live, arch) == 1  # the good one still copied


def test_no_op_when_live_absent(tmp_path):
    assert sync_manifests_to_archive(tmp_path / "nope", tmp_path / "arch") == 0


def test_never_deletes_archive(tmp_path):
    live, arch = tmp_path / "live", tmp_path / "arch"
    keep = _bin(arch, "stale_only_in_archive.bin")
    _bin(live, "1_1_2_3.bin")
    sync_manifests_to_archive(live, arch)
    assert keep.is_file()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/agent/test_manifest_archive.py -q`
Expected: FAIL (`ModuleNotFoundError: orchestrator.agent.manifest_archive`).

- [ ] **Step 3: Implement**

Create `src/orchestrator/agent/manifest_archive.py`:

```python
"""Durable manifest archive — copy SteamPrefill's transient .bin manifests into a
permanent, append-only store so validate can cover the whole prefilled library.

SteamPrefill only writes a manifest when an app has new content (and treats saved
manifests as temporary), so its live cache covers a shrinking subset of the
prefilled library. We snapshot every manifest we see into the archive; validate
reads the union (see manifest_locator). STDLIB ONLY — this module must not import
orchestrator.api / orchestrator.db (agent import-isolation guard,
tests/agent/test_import_isolation.py)."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


def sync_manifests_to_archive(
    live_root: Path, archive_root: Path, *, settle_seconds: float = 10.0
) -> int:
    """Copy .bin files present in live/v1 but not archive/v1 (append-only).

    Preserves mtime (shutil.copy2), skips files written within ``settle_seconds``
    (may be mid-write — picked up next cycle), never deletes from the archive, and
    isolates per-file errors. Returns the number copied. A missing live dir or an
    unwritable archive is a no-op returning 0."""
    live_v1 = live_root / "v1"
    if not live_v1.is_dir():
        return 0
    archive_v1 = archive_root / "v1"
    try:
        archive_v1.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log.warning(
            "manifest_archive.mkdir_failed",
            archive=str(archive_v1),
            reason=f"{type(e).__name__}: {e}"[:200],
        )
        return 0
    existing = {p.name for p in archive_v1.glob("*.bin")}
    now = time.time()
    copied = 0
    for src in live_v1.glob("*.bin"):
        if src.name in existing:
            continue
        try:
            if now - src.stat().st_mtime < settle_seconds:
                continue
            shutil.copy2(src, archive_v1 / src.name)
            copied += 1
        except OSError as e:
            _log.warning(
                "manifest_archive.copy_failed",
                bin=src.name,
                reason=f"{type(e).__name__}: {e}"[:200],
            )
            continue
    if copied:
        _log.info("manifest_archive.synced", copied=copied, archive=str(archive_v1))
    return copied


async def manifest_archive_sync_loop(
    live_root: Path,
    archive_root: Path,
    interval_sec: int,
    *,
    settle_seconds: float = 10.0,
) -> None:
    """Run sync once immediately, then every ``interval_sec`` seconds, forever.

    The sync runs in a worker thread so the event loop is never blocked. Per-cycle
    errors are logged and swallowed (never kill the loop); CancelledError on
    shutdown propagates so the lifespan teardown can await the cancel."""
    while True:
        try:
            await asyncio.to_thread(
                sync_manifests_to_archive,
                live_root,
                archive_root,
                settle_seconds=settle_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let a bad cycle kill the loop
            _log.warning(
                "manifest_archive.loop_error", reason=f"{type(e).__name__}: {e}"[:200]
            )
        await asyncio.sleep(interval_sec)
```

- [ ] **Step 4: Run to verify pass + import-isolation still green**

Run: `.venv/bin/python -m pytest tests/agent/test_manifest_archive.py tests/agent/test_import_isolation.py -q`
Expected: PASS.

---

## Task 4: wire the periodic sync into the agent lifespan

**Files:**
- Modify: `src/orchestrator/agent/app.py`
- Test: `tests/agent/test_app_lifespan.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/agent/test_app_lifespan.py` (match the file's existing settings-construction helper/fixture; if it builds `Settings(...)` inline, mirror that):

```python
import asyncio
import contextlib
from pathlib import Path

from fastapi.testclient import TestClient

import orchestrator.agent.manifest_archive as marc
from orchestrator.agent.app import create_agent_app
from orchestrator.core.settings import Settings


def test_sync_task_wired_when_enabled():
    app = create_agent_app(settings=Settings(manifest_archive_sync_interval_sec=1800))
    with TestClient(app):
        assert len(app.state.agent_bg_tasks) == 1  # the sync loop task


def test_sync_task_absent_when_disabled():
    app = create_agent_app(settings=Settings(manifest_archive_sync_interval_sec=0))
    with TestClient(app):
        assert len(app.state.agent_bg_tasks) == 0


async def test_loop_runs_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(
        marc, "sync_manifests_to_archive", lambda *a, **k: calls.append(1) or 0
    )
    task = asyncio.create_task(
        marc.manifest_archive_sync_loop(Path("/live"), Path("/arch"), 3600)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert calls  # ran once immediately, before the first sleep
```

(`test_loop_runs_immediately` is async — the suite already runs under `pytest-asyncio`; add the module/function marker the file uses, e.g. `pytestmark = pytest.mark.asyncio`, if not auto.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/agent/test_app_lifespan.py -k "sync_task or loop_runs" -q`
Expected: FAIL (`assert len(...) == 1` → 0; the loop import/behavior not wired).

- [ ] **Step 3: Implement the wiring**

In `src/orchestrator/agent/app.py`, add imports near the top:

```python
from pathlib import Path

from orchestrator.agent.manifest_archive import manifest_archive_sync_loop
```

Inside `_lifespan`, after the `prefill_driver` setup block and **before** `try:`/`yield`, add:

```python
        interval = settings.manifest_archive_sync_interval_sec
        if interval > 0:
            sync_task = asyncio.create_task(
                manifest_archive_sync_loop(
                    Path(settings.steam_manifest_cache_dir),
                    Path(settings.steam_manifest_archive_dir),
                    interval,
                )
            )
            app.state.agent_bg_tasks.add(sync_task)
            sync_task.add_done_callback(app.state.agent_bg_tasks.discard)
```

(`asyncio` is already imported. The existing teardown — cancel `agent_bg_tasks`, gather — already cancels this task on shutdown.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/agent/test_app_lifespan.py -q`
Expected: PASS.

---

## Task 5: sweep `full` mode

**Files:**
- Modify: `src/orchestrator/jobs/handlers/sweep.py`
- Test: `tests/jobs/handlers/test_sweep.py` (match existing sweep test location)

- [ ] **Step 1: Write the failing tests**

Add to the sweep test module (mirror its existing fake-`Deps`/fake-`pool` style — these assert which SQL `read_all` receives):

```python
async def test_full_payload_selects_all_steam(monkeypatch):
    captured = {}
    deps = _make_deps(read_all_capture=captured)  # existing helper; see file
    monkeypatch.setattr(sweep_mod, "validator_self_test", _async_true)
    await sweep_mod.sweep_handler({"id": 1, "payload": '{"full": true}'}, deps)
    assert captured["sql"] == sweep_mod._CANDIDATE_SQL_FULL
    assert "status IN" not in captured["sql"]


async def test_default_payload_keeps_status_gated(monkeypatch):
    captured = {}
    deps = _make_deps(read_all_capture=captured)
    monkeypatch.setattr(sweep_mod, "validator_self_test", _async_true)
    await sweep_mod.sweep_handler({"id": 1, "payload": None}, deps)
    assert captured["sql"] == sweep_mod._CANDIDATE_SQL
    assert "status IN ('up_to_date','validation_failed')" in captured["sql"]


async def test_malformed_payload_falls_back_to_gated(monkeypatch):
    captured = {}
    deps = _make_deps(read_all_capture=captured)
    monkeypatch.setattr(sweep_mod, "validator_self_test", _async_true)
    await sweep_mod.sweep_handler({"id": 1, "payload": "not json"}, deps)
    assert captured["sql"] == sweep_mod._CANDIDATE_SQL
```

If the existing sweep tests don't already provide a capturing fake `Deps`/pool, add a minimal one in the test file: a fake pool whose `read_all(sql, *a)` records `captured["sql"] = sql` and returns `[]`, an `agent_client` that is not None, and patch `validator_self_test` to an async `True`. (`_CANDIDATE_SQL` already exists; add `_CANDIDATE_SQL_FULL` in impl.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/jobs/handlers/test_sweep.py -k "payload" -q`
Expected: FAIL (`AttributeError: module … has no attribute '_CANDIDATE_SQL_FULL'`).

- [ ] **Step 3: Implement**

In `src/orchestrator/jobs/handlers/sweep.py`: add `import json` near the top; add the full SQL next to `_CANDIDATE_SQL`:

```python
_CANDIDATE_SQL_FULL = "SELECT id, status FROM games WHERE platform='steam' ORDER BY id"
```

In `sweep_handler`, after the `validator_self_test` gate and before `rows = await deps.pool.read_all(...)`, replace the read with:

```python
    try:
        full = bool(json.loads(job.get("payload") or "{}").get("full", False))
    except (json.JSONDecodeError, TypeError, AttributeError):
        full = False
    candidate_sql = _CANDIDATE_SQL_FULL if full else _CANDIDATE_SQL
    rows = await deps.pool.read_all(candidate_sql)
    _log.info("sweep.started", job_id=job_id, candidates=len(rows), full=full)
```

(Remove the old `rows = …` + `sweep.started` log lines being replaced. The semaphore, `_one`, gather, and completion log are unchanged. A no-manifest game still returns `outcome="error"` from `validate_one_game`, which is excluded from `_STATUS_FOR` — status stays unchanged; this is covered by existing `tests/jobs/handlers/test_validate.py`.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/jobs/handlers/test_sweep.py -q`
Expected: PASS.

---

## Task 6: enqueue `full` + `POST /api/v1/sweep` trigger

**Files:**
- Modify: `src/orchestrator/scheduler/jobs.py` (`enqueue_validation_sweep`)
- Create: `src/orchestrator/api/routers/sweep_trigger.py`
- Modify: `src/orchestrator/api/main.py` (register router)
- Test: `tests/scheduler/test_jobs.py` (enqueue), `tests/api/test_sweep_trigger.py` (endpoint)

- [ ] **Step 1: Write the failing tests**

Enqueue test (mirror the existing `enqueue_validation_sweep` test setup with a real test pool):

```python
async def test_enqueue_sweep_full_writes_payload(pool):
    n = await enqueue_validation_sweep(pool, full=True, source="api")
    assert n == 1
    row = await pool.read_one("SELECT payload, source FROM jobs WHERE kind='sweep'")
    assert row["payload"] == '{"full": true}'
    assert row["source"] == "api"


async def test_enqueue_sweep_default_no_payload(pool):
    await enqueue_validation_sweep(pool)
    row = await pool.read_one("SELECT payload, source FROM jobs WHERE kind='sweep'")
    assert row["payload"] is None
    assert row["source"] == "scheduler"
```

Endpoint test (mirror `tests/api/test_validate_trigger.py` client/pool fixture):

```python
def test_sweep_trigger_full(client):  # client = TestClient over a test app+pool
    r = client.post("/api/v1/sweep", json={"full": True})
    assert r.status_code == 202
    body = r.json()
    assert body["full"] is True and isinstance(body["job_id"], int)


def test_sweep_trigger_default_false(client):
    r = client.post("/api/v1/sweep")
    assert r.status_code == 202
    assert r.json()["full"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/scheduler/test_jobs.py -k sweep_full tests/api/test_sweep_trigger.py -q`
Expected: FAIL (`TypeError: enqueue_validation_sweep() got an unexpected keyword 'full'`; `404` on `/api/v1/sweep`).

- [ ] **Step 3a: Implement enqueue**

Replace `enqueue_validation_sweep` in `src/orchestrator/scheduler/jobs.py`:

```python
async def enqueue_validation_sweep(
    pool: Pool, *, full: bool = False, source: str = "scheduler"
) -> int:
    """Insert a `sweep` job row if none is queued/running (F13).

    ``full=True`` validates EVERY steam game (the validate-all backfill), carried
    on the job payload `{"full": true}`; the weekly cron uses the default
    (status-gated) sweep. Mirrors `enqueue_library_sync`: at most one in-flight
    sweep, DB-enforced by `idx_jobs_sweep_inflight` via `ON CONFLICT DO NOTHING`.
    Returns the rowcount (1 queued / 0 deduped-or-failed). Never raises."""
    payload = '{"full": true}' if full else None
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, state, source, payload) "
            "VALUES ('sweep', 'queued', ?, ?) ON CONFLICT DO NOTHING",
            (source, payload),
        )
        if inserted:
            _log.info("scheduler.sweep.queued", full=full, source=source)
        else:
            _log.info("scheduler.sweep.dedup_skip")
        return inserted
    except PoolError as e:
        _log.error("scheduler.sweep.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        _log.error(
            "scheduler.sweep.unexpected_error",
            error=type(e).__name__,
            reason=str(e)[:200],
        )
        return 0
```

(The APScheduler callback still calls `enqueue_validation_sweep(pool)` — defaults give the prior behavior, now with an explicit `source='scheduler'` and `payload=NULL`.)

- [ ] **Step 3b: Implement the endpoint**

Create `src/orchestrator/api/routers/sweep_trigger.py`:

```python
"""POST /api/v1/sweep — manually enqueue a validation sweep (F13).

`{"full": true}` runs the validate-all backfill over EVERY steam game (used after
seeding the durable manifest archive); the default re-validates only the cached
library, same as the weekly cron. Reuses the sweep in-flight dedup."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
from orchestrator.scheduler.jobs import enqueue_validation_sweep

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["sweep"])


class SweepTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full: bool = False


@router.post(
    "/sweep",
    responses={
        202: {"description": "Sweep queued (or existing in-flight sweep returned)"},
        401: {"description": "Missing/invalid bearer"},
        503: {"description": "Database unavailable"},
    },
)
async def trigger_sweep(
    body: SweepTriggerRequest | None = None,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008
) -> JSONResponse:
    full = bool(body.full) if body is not None else False
    try:
        await enqueue_validation_sweep(pool, full=full, source="api")
        row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='sweep' "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1"
        )
        if row is None:
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("sweep_trigger.queued", job_id=int(row["id"]), full=full)
        return JSONResponse(status_code=202, content={"job_id": int(row["id"]), "full": full})
    except PoolError as e:
        _log.error("sweep_trigger.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
```

- [ ] **Step 3c: Register the router** in `src/orchestrator/api/main.py`:

Add the import alongside the others (~line 38):

```python
from orchestrator.api.routers.sweep_trigger import router as sweep_trigger_router
```

Add the include alongside the others (after `validate_trigger_router`, ~line 427):

```python
    app.include_router(sweep_trigger_router)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/scheduler/test_jobs.py tests/api/test_sweep_trigger.py -q`
Expected: PASS.

---

## Task 7: `orchestrator-cli cache validate-all`

**Files:**
- Create: `src/orchestrator/cli/commands/cache.py`
- Modify: `src/orchestrator/cli/main.py` (register `cache` group)
- Test: `tests/cli/test_cache.py` (mirror existing CLI test pattern, e.g. `tests/cli/test_game.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/cli/test_cache.py` (mirror how other CLI tests stub `OrchClient`/`make_client`; example shape):

```python
from click.testing import CliRunner

from orchestrator.cli.main import cli


def test_cache_validate_all_posts_full_sweep(monkeypatch):
    posted = {}

    class FakeClient:
        def post(self, path, json=None):
            posted["path"] = path
            posted["json"] = json
            return {"job_id": 42, "full": True}

    monkeypatch.setattr("orchestrator.cli.commands.cache.make_client", lambda ctx: FakeClient())
    result = CliRunner().invoke(cli, ["cache", "validate-all"])
    assert result.exit_code == 0
    assert posted["path"] == "/api/v1/sweep"
    assert posted["json"] == {"full": True}
    assert "42" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/cli/test_cache.py -q`
Expected: FAIL (`No such command 'cache'`).

- [ ] **Step 3: Implement**

Create `src/orchestrator/cli/commands/cache.py`:

```python
"""``cache`` subcommands — cache-maintenance operations (F11)."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client


@click.group()
def cache() -> None:
    """Cache-maintenance operations."""


@cache.command("validate-all")
@click.pass_context
@handles_api_errors
def cache_validate_all(ctx: click.Context) -> None:
    """Enqueue a full validation sweep over EVERY steam game (backfill).

    Use after seeding the durable manifest archive so genuinely-cached games are
    re-checked and flip to up_to_date."""
    client = make_client(ctx)
    resp = client.post("/api/v1/sweep", json={"full": True})
    output.success(f"queued full validation sweep (job_id={resp['job_id']}).")
```

Register in `src/orchestrator/cli/main.py`: add `cache` to the import line and `cli.add_command(cache.cache)`:

```python
from orchestrator.cli.commands import auth, cache, config, db, game, jobs, library, status
```
```python
cli.add_command(cache.cache)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/cli/test_cache.py -q`
Expected: PASS.

---

## Task 8: Full verification + single commit (controller)

- [ ] **Step 1: Format + lint + type-check**

```bash
.venv/bin/ruff format src tests
.venv/bin/ruff check src tests
.venv/bin/mypy src/orchestrator
```
Expected: ruff clean; mypy clean (watch for bare-dict returns needing `dict[str, Any]`; no `assert` in `src`).

- [ ] **Step 2: Full suite**

Run: `.venv/bin/python -m pytest -q --ignore=tests/scripts`
Expected: all pass except the known `tests/test_licenses.py` (pip-licenses) failure.

- [ ] **Step 3: enforce-evaluate + commit (controller)**

Present the implementation evaluation, run `scripts/mark-evaluated.sh`, then **present commit-structure options A/B/C and wait** before the single `feat` commit. Karl opens/merges the PR.

Suggested message:
```
feat(steam): durable manifest archive + validate-all backfill

Agent copies SteamPrefill's transient .bin manifests into an append-only
archive (validate reads live∪archive, newest-per-depot); a periodic agent
task keeps it current. Sweep gains a `full` mode (jobs.payload) that
validates every steam game, triggered by POST /api/v1/sweep and
`orchestrator-cli cache validate-all`. Fixes the ~747 prefilled apps that
had no manifest to validate against (see
docs/superpowers/specs/2026-06-24-durable-manifest-store-design.md).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Operator runbook (POST-MERGE — Claude runs on the boxes; not code tasks)

1. **Mount the archive.** Add `-v orchestrator-manifests:/manifest-archive` to `/home/karl/deploy-agent.sh`; recreate the agent. (`ORCH_STEAM_MANIFEST_ARCHIVE_DIR` defaults to `/manifest-archive`.) The named volume survives recreation.
2. **Seed.** Compute `missing = selectedAppsToPrefill ∖ archived-apps`; run `SteamPrefill prefill --force <batch>` for those app_ids in throttled off-hours batches (LAN re-read → HITs, no WAN), not overlapping the root cron. The background sync archives each batch's manifests. Repeat until `missing` is empty.
3. **Backfill.** `orchestrator-cli cache validate-all` once (consider a lower `ORCH_SWEEP_BATCH_SIZE` for this run given NAS load). Genuinely-cached → `up_to_date`, partial/missing → `validation_failed`, no-manifest → unchanged. This also corrects the wrongly-`not_downloaded` rows.

---

## Self-Review

**Spec coverage:** A (archive + union read) → Tasks 1,2. B (sync + periodic) → Tasks 3,4. C (full sweep + trigger) → Tasks 5,6,7. Settings → Task 1. Deploy/seed/backfill → operator runbook. Import-isolation preserved → Task 3 (stdlib-only) verified in Task 3 Step 4. All spec sections map to a task.

**Placeholder scan:** none — every code step has full code; test steps have full test bodies.

**Type consistency:** `cache_roots: list[Path]` used identically in Tasks 2 (definition) and the Task 2 caller edit; `sync_manifests_to_archive(live_root, archive_root, *, settle_seconds)` and `manifest_archive_sync_loop(live_root, archive_root, interval_sec, *, settle_seconds)` consistent across Tasks 3,4; `enqueue_validation_sweep(pool, *, full, source)` consistent across Tasks 6 (def) and the sweep_trigger caller; `_CANDIDATE_SQL_FULL` consistent across Tasks 5 (def) and its test.
