# F7 Cache Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate whether a Steam game's depot-manifest chunks are present in the lancache on-disk cache by computing each chunk's nginx cache path and stat-ing it; record a `validation_history` row and update `games.status`.

**Architecture:** Pure cache-key derivation (`validator/cache_key.py`) + a stat engine (`validator/disk_stat.py`) in the orchestrator process; manifest protobuf expansion in the worker venv (`manifest.expand` IPC op, offline); a `validate` job handler and a bearer-gated trigger endpoint; a startup self-test gating `health.validator_healthy`. A `manifests.depot_id` column (migration 0003) supplies the depot id F7 needs.

**Tech Stack:** FastAPI, Pydantic v2, aiosqlite, structlog, zstandard, steam-next (worker venv), hashlib.md5.

**Process note:** This project gates commits behind `scripts/process-checklist.sh` (6 Build-Loop steps) and uses a single combined `feat` commit per feature, NOT per-task commits. The design spec + spike are already committed (566e741). Implement all tasks, then run the full gate sweep and the combined commit at the end (commit structure confirmed with the Orchestrator first). Per-task "Commit" steps from the generic template are intentionally omitted.

**Golden vector (spike A4) — must reproduce exactly:**
- `steam_chunk_uri(529345, "c8e5d44ca8618200552eb754ff6f6922c92a54ff")` → `/depot/529345/chunk/c8e5d44ca8618200552eb754ff6f6922c92a54ff`
- `cache_key("steam", uri, "bytes=0-10485759")` → `22e7d56f787714bc78e23495d93da0db`
- `cache_path(Path("/data/cache/cache"), "22e7d56f787714bc78e23495d93da0db", "2:2")` → `/data/cache/cache/db/a0/22e7d56f787714bc78e23495d93da0db`

---

## Task 1: Migration 0003 — `manifests.depot_id`

**Files:**
- Create: `src/orchestrator/db/migrations/0003_manifests_depot_id.sql`
- Modify: `src/orchestrator/db/migrations/CHECKSUMS`
- Modify: `src/orchestrator/jobs/handlers/manifest_fetch.py` (store depot_id)
- Test: `tests/jobs/test_manifest_fetch_handler.py` (assert depot_id stored), `tests/db/` (migration applies)

- [ ] **Step 1: Write the failing test** — extend `test_manifest_fetch_handler.py` happy path to assert `depot_id`:

```python
async def test_stores_depot_id(self, pool):
    game_id = await _seed_game(pool)
    stub = _StubSteam(result={"manifests": [
        _fake_manifest_payload(731, 100, "d1", 1000, 10),
    ]})
    await manifest_fetch_handler(_job(game_id), Deps(pool=pool, steam_client=stub))
    row = await pool.read_one("SELECT depot_id FROM manifests WHERE game_id=?", (game_id,))
    assert row["depot_id"] == 731
```

- [ ] **Step 2: Run it — expect FAIL** (`no such column: depot_id`)

Run: `.venv/bin/pytest tests/jobs/test_manifest_fetch_handler.py::TestHappyPath::test_stores_depot_id -q`

- [ ] **Step 3: Write the migration** (`ALTER TABLE` is STRICT-safe for a nullable column; no rebuild):

```sql
-- 0003_manifests_depot_id.sql
-- F7: add depot_id so the validator can build chunk URLs and pick the
-- latest manifest per depot. Nullable (no backfill — no live manifest
-- data exists yet); the BL12 handler populates it going forward.
ALTER TABLE manifests ADD COLUMN depot_id INTEGER;

CREATE INDEX idx_manifests_game_depot
    ON manifests(game_id, depot_id, fetched_at DESC);
```

- [ ] **Step 4: Regenerate the checksum** and append/replace the CHECKSUMS line:

Run: `shasum -a 256 src/orchestrator/db/migrations/0003_manifests_depot_id.sql`
Then add a line to `CHECKSUMS`: `0003  <hash>  0003_manifests_depot_id.sql`

- [ ] **Step 5: Update the BL12 handler to populate depot_id.** In `manifest_fetch.py`, change `_UPSERT_SQL` to include `depot_id` and add it to the bound tuple:

```python
_UPSERT_SQL = (
    "INSERT INTO manifests "
    "(game_id, depot_id, version, fetched_at, chunk_count, total_bytes, raw) "
    "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?) "
    "ON CONFLICT(game_id, version) DO UPDATE SET "
    "  depot_id = excluded.depot_id, "
    "  fetched_at = CURRENT_TIMESTAMP, "
    "  chunk_count = excluded.chunk_count, "
    "  total_bytes = excluded.total_bytes, "
    "  raw = excluded.raw"
)
```

In the loop, bind `int(depot_id)`:
```python
await deps.pool.execute_write(
    _UPSERT_SQL,
    (game_id, int(depot_id), str(gid), int(chunk_count), int(total_bytes), raw_bytes),
)
```

- [ ] **Step 6: Run migration + handler tests — expect PASS**

Run: `.venv/bin/pytest tests/db tests/jobs/test_manifest_fetch_handler.py -q`

---

## Task 2: Settings — identifier + expand timeout

**Files:**
- Modify: `src/orchestrator/core/settings.py`
- Test: `tests/core/test_settings.py`

- [ ] **Step 1: Write failing tests:**

```python
def test_steam_cache_identifier_default():
    assert build_settings().steam_cache_identifier == "steam"

def test_manifest_expand_timeout_default_and_bounds():
    assert build_settings().steam_worker_manifest_expand_timeout_sec == 120
    with pytest.raises(ValidationError):
        Settings(steam_worker_manifest_expand_timeout_sec=10)
    with pytest.raises(ValidationError):
        Settings(steam_worker_manifest_expand_timeout_sec=601)
```
(Use the existing test module's helpers/imports for `build_settings`/`Settings`/`ValidationError`.)

- [ ] **Step 2: Run — expect FAIL** (`AttributeError`)

- [ ] **Step 3: Add fields** near the other steam-worker timeouts in `settings.py`:

```python
    steam_cache_identifier: str = "steam"
    steam_worker_manifest_expand_timeout_sec: int = Field(default=120, ge=30, le=600)
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/core/test_settings.py -q`

---

## Task 3: `validator/cache_key.py` — pure derivation (golden vectors)

**Files:**
- Create: `src/orchestrator/validator/__init__.py` (if empty/missing — it exists per research)
- Create: `src/orchestrator/validator/cache_key.py`
- Test: `tests/validator/__init__.py`, `tests/validator/test_cache_key.py`

- [ ] **Step 1: Write the failing tests** (golden vectors + levels generalization + input validation):

```python
from pathlib import Path
import pytest
from orchestrator.validator.cache_key import (
    steam_chunk_uri, slice_range_zero, cache_key, cache_path,
)

SHA = "c8e5d44ca8618200552eb754ff6f6922c92a54ff"

def test_golden_vector_full_chain():
    uri = steam_chunk_uri(529345, SHA)
    assert uri == f"/depot/529345/chunk/{SHA}"
    h = cache_key("steam", uri, slice_range_zero(10_485_760))
    assert h == "22e7d56f787714bc78e23495d93da0db"
    p = cache_path(Path("/data/cache/cache"), h, "2:2")
    assert p == Path("/data/cache/cache/db/a0/22e7d56f787714bc78e23495d93da0db")

def test_slice_range_zero():
    assert slice_range_zero(10_485_760) == "bytes=0-10485759"
    assert slice_range_zero(1_048_576) == "bytes=0-1048575"

def test_levels_generalization():
    h = "0123456789abcdef0123456789abcdef"
    assert cache_path(Path("/c"), h, "2:2") == Path(f"/c/ef/cd/{h}")
    assert cache_path(Path("/c"), h, "1:2") == Path(f"/c/f/de/{h}")
    assert cache_path(Path("/c"), h, "1:1:1") == Path(f"/c/f/e/d/{h}")

def test_rejects_bad_sha():
    with pytest.raises(ValueError):
        steam_chunk_uri(1, "NOTHEX")
    with pytest.raises(ValueError):
        steam_chunk_uri(1, "abc")  # too short

def test_rejects_negative_depot():
    with pytest.raises(ValueError):
        steam_chunk_uri(-1, SHA)
```

- [ ] **Step 2: Run — expect FAIL** (module missing)

Run: `.venv/bin/pytest tests/validator/test_cache_key.py -q`

- [ ] **Step 3: Implement `cache_key.py`:**

```python
"""Pure lancache cache-key derivation (F7). See spikes/spike_a4_lancache_cache_key.md.

No I/O, no settings import — the caller supplies config. The nginx cache
key is md5(identifier + uri + slice_range); the on-disk path consumes hex
from the END of the md5 per the `levels` directive.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def steam_chunk_uri(depot_id: int, sha_hex: str) -> str:
    """URI nginx caches a Steam depot chunk under: /depot/<id>/chunk/<sha>."""
    if depot_id < 0:
        raise ValueError(f"depot_id must be >= 0, got {depot_id}")
    if not _SHA_RE.match(sha_hex):
        raise ValueError(f"sha_hex must be 40 lowercase hex chars, got {sha_hex!r}")
    return f"/depot/{depot_id}/chunk/{sha_hex}"


def slice_range_zero(slice_size: int) -> str:
    """The first (and only, for sub-slice chunks) slice range header value."""
    if slice_size <= 0:
        raise ValueError("slice_size must be > 0")
    return f"bytes=0-{slice_size - 1}"


def cache_key(identifier: str, uri: str, slice_range: str) -> str:
    """md5(identifier + uri + slice_range) as 32-char lowercase hex."""
    return hashlib.md5(
        f"{identifier}{uri}{slice_range}".encode(), usedforsecurity=False
    ).hexdigest()


def cache_path(cache_root: Path, h: str, levels: str) -> Path:
    """nginx cache file path for md5 hex `h` under `levels` (e.g. "2:2").

    nginx consumes hex chars from the END: for levels L1:L2:..:Ln the last
    directory uses the final Ln chars, the prior dir the Ln-1 chars before
    that, and so on.
    """
    if not _HEX32.match(h):
        raise ValueError(f"expected 32-char md5 hex, got {h!r}")
    widths = [int(x) for x in levels.split(":")]
    parts: list[str] = []
    end = len(h)
    for w in widths:
        parts.append(h[end - w:end])
        end -= w
    return cache_root.joinpath(*parts, h)


_HEX32 = re.compile(r"^[0-9a-f]{32}$")
```

- [ ] **Step 4: Run — expect PASS** (all golden vectors)

Run: `.venv/bin/pytest tests/validator/test_cache_key.py -q`

---

## Task 4: Worker IPC `manifest.expand`

**Files:**
- Modify: `src/orchestrator/platform/steam/worker.py` (`_handle_manifest_expand` + register)
- Modify: `src/orchestrator/platform/steam/client.py` (`manifest_expand` + timeout)
- Test: `tests/platform/steam/test_client_unit.py` (round-trip + timeout)

- [ ] **Step 1: Write the failing client test** (mirror BL12 `TestManifestFetch`):

```python
class TestManifestExpand:
    async def test_round_trip(self, monkeypatch):
        client = _make_client_with_fake_transport(
            response={"depot_id": 731, "chunk_shas": ["aa"*20, "bb"*20]}
        )
        out = await client.manifest_expand(b"\x28\xb5rawbytes")
        assert out["depot_id"] == 731
        assert out["chunk_shas"] == ["aa"*20, "bb"*20]
        # sent base64 of the raw bytes under "raw_b64"
        sent = client._last_sent_params  # test transport records this
        import base64
        assert base64.b64decode(sent["raw_b64"]) == b"\x28\xb5rawbytes"
```
(Adapt to the existing fake-transport harness used by `TestManifestFetch` in this file.)

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: manifest_expand`)

- [ ] **Step 3: Add the client method + timeout** in `client.py`. In the timeout-overrides dict add:
```python
            "manifest.expand": float(settings.steam_worker_manifest_expand_timeout_sec),
```
And the method (near `manifest_fetch`):
```python
    async def manifest_expand(self, raw: bytes) -> dict[str, Any]:
        """Deserialize a stored manifest BLOB in the worker venv (offline).

        Returns {"depot_id": int, "chunk_shas": [hex, ...]}. No Steam
        session required — pure protobuf parse.
        """
        import base64
        return await self._send_and_await(
            "manifest.expand", {"raw_b64": base64.b64encode(raw).decode("ascii")}
        )
```

- [ ] **Step 4: Implement the worker handler** in `worker.py` (near `_handle_manifest_fetch`):

```python
def _handle_manifest_expand(msg_id: str, params: dict[str, str]) -> None:
    """Deserialize a stored manifest BLOB → {depot_id, chunk_shas}.

    Offline: zstd-decompress then DepotManifest(data). No CDNClient, no
    auth. Chunk SHAs are deduped (the same chunk can appear in multiple
    file mappings).
    """
    import base64

    import zstandard
    from steam.core.manifest import DepotManifest

    raw_b64 = params.get("raw_b64")
    if not raw_b64:
        _err(msg_id, "InvalidArgument", "manifest.expand requires raw_b64")
        return
    try:
        compressed = base64.b64decode(raw_b64)
        data = zstandard.ZstdDecompressor().decompress(compressed)
        mfst = DepotManifest(data)
        seen: dict[str, None] = {}
        for mapping in mfst.payload.mappings:
            for chunk in mapping.chunks:
                seen.setdefault(chunk.sha.hex(), None)
        _ok(msg_id, {"depot_id": int(mfst.depot_id), "chunk_shas": list(seen)})
    except Exception as e:  # noqa: BLE001 — report parse failures to caller
        _err(msg_id, "ManifestParseError", f"{type(e).__name__}: {e}"[:200])
```

Register in `_HANDLERS`:
```python
    "manifest.expand": _handle_manifest_expand,
```

- [ ] **Step 5: Write a worker-handler unit test** in `test_client_unit.py` (or a worker test module) that drives `_handle_manifest_expand` with monkeypatched `zstandard` + `DepotManifest` returning a fake manifest whose `payload.mappings[*].chunks[*].sha` yields duplicate SHAs; assert dedup + depot_id. Use the `_ok`/`_err` capture pattern already used by BL12 worker tests.

- [ ] **Step 6: Run — expect PASS**

Run: `.venv/bin/pytest tests/platform/steam/test_client_unit.py -q`

---

## Task 5: `validator/disk_stat.py` — stat engine + orchestration

**Files:**
- Create: `src/orchestrator/validator/disk_stat.py`
- Test: `tests/validator/test_disk_stat.py`

- [ ] **Step 1: Write failing tests for `validate_chunks`** (tmp cache tree):

```python
import asyncio
from pathlib import Path
import pytest
from orchestrator.validator.disk_stat import validate_chunks

pytestmark = pytest.mark.asyncio

async def test_counts_cached_and_missing(tmp_path):
    present = tmp_path / "a"; present.write_bytes(b"x")
    empty = tmp_path / "b"; empty.write_bytes(b"")
    absent = tmp_path / "c"
    cached, missing = await validate_chunks([present, empty, absent])
    assert (cached, missing) == (1, 2)   # empty file counts as missing

async def test_batch_boundary(tmp_path):
    paths = []
    for i in range(300):
        p = tmp_path / f"f{i}"; p.write_bytes(b"x"); paths.append(p)
    cached, missing = await validate_chunks(paths, batch_size=256)
    assert (cached, missing) == (300, 0)
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `validate_chunks` + `validate_game`:**

```python
"""F7 disk-stat validator engine."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.validator.cache_key import (
    cache_key, cache_path, slice_range_zero, steam_chunk_uri,
)

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


@dataclass
class ValidationResult:
    chunks_total: int
    chunks_cached: int
    chunks_missing: int
    outcome: str  # cached | partial | missing | error
    manifest_version: str
    error: str | None = None


def _stat_batch(paths: list[Path]) -> int:
    cached = 0
    for p in paths:
        try:
            if p.stat().st_size > 0:
                cached += 1
        except OSError:
            pass
    return cached


async def validate_chunks(paths: list[Path], *, batch_size: int = 256) -> tuple[int, int]:
    """Return (cached, missing). Cached = exists AND st_size > 0."""
    loop = asyncio.get_running_loop()
    cached = 0
    for i in range(0, len(paths), batch_size):
        batch = paths[i:i + batch_size]
        cached += await loop.run_in_executor(None, _stat_batch, batch)
    return cached, len(paths) - cached


def _classify(total: int, cached: int) -> str:
    if total == 0:
        return "error"
    if cached == total:
        return "cached"
    if cached == 0:
        return "missing"
    return "partial"


async def validate_game(
    pool: Any, deps: Deps, game_id: int, settings: Settings
) -> ValidationResult:
    """Validate the latest manifest per depot for `game_id`."""
    cache_root = Path(settings.lancache_nginx_cache_path)
    if not cache_root.is_dir():
        return ValidationResult(0, 0, 0, "error", "", f"cache root not a dir: {cache_root}")

    # Latest manifest row per depot (max fetched_at, tie-break max id).
    rows = await pool.read_all(
        "SELECT m.depot_id, m.version, m.raw FROM manifests m "
        "WHERE m.game_id = ? AND m.depot_id IS NOT NULL AND m.id IN ("
        "  SELECT id FROM manifests m2 WHERE m2.game_id = m.game_id "
        "  AND m2.depot_id = m.depot_id ORDER BY fetched_at DESC, id DESC LIMIT 1"
        ") ORDER BY m.depot_id",
        (game_id,),
    )
    if not rows:
        return ValidationResult(0, 0, 0, "error", "", "no manifests; run manifest fetch first")

    slice_range = slice_range_zero(settings.cache_slice_size_bytes)
    identifier = settings.steam_cache_identifier
    levels = settings.cache_levels
    seen: set[tuple[int, str]] = set()
    paths: list[Path] = []
    versions: list[str] = []
    for row in rows:
        depot_id = int(row["depot_id"])
        versions.append(f"{depot_id}:{row['version']}")
        expanded = await deps.steam_client.manifest_expand(row["raw"])
        for sha in expanded.get("chunk_shas", []):
            key = (depot_id, sha)
            if key in seen:
                continue
            seen.add(key)
            uri = steam_chunk_uri(depot_id, sha)
            h = cache_key(identifier, uri, slice_range)
            paths.append(cache_path(cache_root, h, levels))

    cached, missing = await validate_chunks(paths)
    total = len(paths)
    outcome = _classify(total, cached)
    return ValidationResult(
        total, cached, missing, outcome, ",".join(sorted(versions))
    )
```

- [ ] **Step 4: Add `validate_game` tests** (seed manifest rows in `pool`, stub `deps.steam_client.manifest_expand`, build a tmp cache tree so some chunk paths exist):

```python
async def test_validate_game_cached(pool, tmp_path, monkeypatch):
    # seed a steam game + one manifest row (raw bytes irrelevant; expand is stubbed)
    # stub deps.steam_client.manifest_expand -> {"depot_id":731,"chunk_shas":[SHA]}
    # precreate the cache file at the derived path with content
    # assert result.outcome == "cached" and chunks_total == 1
    ...
```
(Use the project's `pool` fixture; construct `Deps` with a stub steam_client; set `settings.lancache_nginx_cache_path = tmp_path` via a Settings override or monkeypatch. Derive the expected path with `cache_key`/`cache_path` to create the file.)

Cover: cached, partial (one chunk present, one absent), missing (none), no-manifests → error, cache-root-missing → error.

- [ ] **Step 5: Run — expect PASS**

Run: `.venv/bin/pytest tests/validator/test_disk_stat.py -q`

---

## Task 6: `validate` job handler

**Files:**
- Create: `src/orchestrator/jobs/handlers/validate.py`
- Modify: `src/orchestrator/jobs/handlers/__init__.py` (register)
- Test: `tests/jobs/test_validate_handler.py`

- [ ] **Step 1: Write failing tests** — for each outcome assert a `validation_history` row + `games.status`:

```python
async def test_cached_marks_up_to_date(pool, tmp_path, monkeypatch):
    # seed game + manifest; stub expand; create cache files so all chunks present
    await validate_handler(_job(game_id), Deps(pool=pool, steam_client=stub))
    vh = await pool.read_one("SELECT method, outcome, chunks_total FROM validation_history WHERE game_id=?", (game_id,))
    assert vh["method"] == "disk_stat"
    assert vh["outcome"] == "cached"
    g = await pool.read_one("SELECT status, last_validated_at FROM games WHERE id=?", (game_id,))
    assert g["status"] == "up_to_date"
    assert g["last_validated_at"] is not None

async def test_missing_marks_validation_failed(...):  # status == "validation_failed"
async def test_error_leaves_status_unchanged(...):     # no cache root → status stays
async def test_non_steam_raises(pool):                 # ValueError
async def test_unknown_game_raises(pool):              # ValueError
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `validate.py`:**

```python
"""F7 — validate job handler."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from orchestrator.core.settings import get_settings
from orchestrator.validator.disk_stat import validate_game

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)

_INSERT_VH = (
    "INSERT INTO validation_history "
    "(game_id, manifest_version, started_at, finished_at, method, "
    " chunks_total, chunks_cached, chunks_missing, outcome, error) "
    "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 'disk_stat', ?, ?, ?, ?, ?)"
)

_STATUS_FOR = {"cached": "up_to_date", "partial": "validation_failed",
               "missing": "validation_failed"}


async def validate_handler(job: dict[str, Any], deps: Deps) -> None:
    if job.get("platform") != "steam":
        raise ValueError(f"validate only supports steam (got {job.get('platform')!r})")
    if deps.steam_client is None:
        raise RuntimeError("steam_client required for validate handler")
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("validate job has no game_id")
    game = await deps.pool.read_one("SELECT id, platform FROM games WHERE id=?", (game_id,))
    if game is None:
        raise ValueError(f"game {game_id} not found")
    if game["platform"] != "steam":
        raise ValueError(f"game {game_id} is {game['platform']!r}, not steam")

    started = await deps.pool.read_one("SELECT CURRENT_TIMESTAMP AS t")
    settings = get_settings()
    _log.info("validate.started", job_id=job.get("id"), game_id=game_id)
    result = await validate_game(deps.pool, deps, game_id, settings)

    await deps.pool.execute_write(
        _INSERT_VH,
        (game_id, result.manifest_version, started["t"], result.chunks_total,
         result.chunks_cached, result.chunks_missing, result.outcome,
         (result.error or None)),
    )
    new_status = _STATUS_FOR.get(result.outcome)
    if new_status is not None:
        await deps.pool.execute_write(
            "UPDATE games SET status=?, last_validated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, game_id),
        )
    _log.info("validate.recorded", job_id=job.get("id"), game_id=game_id,
              outcome=result.outcome, total=result.chunks_total,
              cached=result.chunks_cached)
```

Register in `handlers/__init__.py`:
```python
    from orchestrator.jobs.handlers.validate import validate_handler
    register("validate", validate_handler)
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/jobs/test_validate_handler.py -q`

---

## Task 7: Trigger endpoint `POST /api/v1/games/{game_id}/validate`

**Files:**
- Create: `src/orchestrator/api/routers/validate_trigger.py`
- Modify: `src/orchestrator/api/main.py` (include router)
- Test: `tests/api/test_validate_trigger_router.py`

- [ ] **Step 1: Write failing tests** — copy `tests/api/test_manifest_trigger_router.py` structure, swapping `manifest/fetch` → `validate` and `kind='manifest_fetch'` → `kind='validate'`. Cover: 202 queue, dedup same job_id, distinct games, new job after finished, 404 unknown, 400 non-steam, 401 missing/wrong bearer, 503 PoolError.

- [ ] **Step 2: Run — expect FAIL** (404 route missing)

- [ ] **Step 3: Implement the router** (copy `manifest_trigger.py`, swap kind + path + log names):

```python
"""POST /api/v1/games/{game_id}/validate — F7 validate trigger."""
from __future__ import annotations
from typing import TYPE_CHECKING
import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
if TYPE_CHECKING:
    from orchestrator.db.pool import Pool
_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/games", tags=["validate"])

@router.post("/{game_id}/validate", responses={
    202: {"description": "Validate job queued or existing in-flight job returned"},
    400: {"description": "Game is on a non-steam platform"},
    404: {"description": "Game not found"},
    503: {"description": "Database unavailable"}})
async def trigger_validate(game_id: int, pool: Pool = Depends(get_pool_dep)) -> JSONResponse:  # noqa: B008
    try:
        game = await pool.read_one("SELECT id, platform FROM games WHERE id=?", (game_id,))
        if game is None:
            raise HTTPException(status_code=404, detail=f"game {game_id} not found")
        if game["platform"] != "steam":
            raise HTTPException(status_code=400, detail=f"validate only supports steam (got {game['platform']!r})")
        existing = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='validate' AND game_id=? "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1", (game_id,))
        if existing is not None:
            return JSONResponse(status_code=202, content={"job_id": int(existing["id"])})
        await pool.execute_write(
            "INSERT INTO jobs (kind, game_id, platform, state, source) "
            "VALUES ('validate', ?, 'steam', 'queued', 'api')", (game_id,))
        new_row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='validate' AND game_id=? "
            "AND state='queued' ORDER BY id DESC LIMIT 1", (game_id,))
        if new_row is None:
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("validate_trigger.queued", game_id=game_id, job_id=int(new_row["id"]))
        return JSONResponse(status_code=202, content={"job_id": int(new_row["id"])})
    except HTTPException:
        raise
    except PoolError as e:
        _log.error("validate_trigger.db_unavailable", game_id=game_id, reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
```

Wire in `main.py` (mirror the manifest_trigger include line):
```python
    from orchestrator.api.routers import validate_trigger
    app.include_router(validate_trigger.router)
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest tests/api/test_validate_trigger_router.py -q`

---

## Task 8: Startup self-test → `health.validator_healthy`

**Files:**
- Create: `src/orchestrator/validator/self_test.py`
- Modify: `src/orchestrator/api/main.py` (lifespan sets `app.state.validator_healthy`)
- Modify: `src/orchestrator/api/routers/health.py` (read it into `all_healthy`)
- Test: `tests/api/test_health_endpoint.py`, `tests/api/test_lifespan*.py`, `tests/validator/test_self_test.py`

- [ ] **Step 1: Write failing tests:**

```python
# tests/validator/test_self_test.py
async def test_self_test_true_when_cache_dir_ok(tmp_path):
    s = build_settings(lancache_nginx_cache_path=tmp_path)
    assert await validator_self_test(s) is True

async def test_self_test_false_when_cache_dir_missing(tmp_path):
    s = build_settings(lancache_nginx_cache_path=tmp_path / "nope")
    assert await validator_self_test(s) is False
```
And in `test_health_endpoint.py`: with `app.state.validator_healthy = True` and all other subsystems healthy, `/health` → 200; with it `False`, `/health` → 503 and body `validator_healthy is False`.

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `self_test.py`:**

```python
"""F7 startup self-test gating health.validator_healthy."""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING
import structlog
from orchestrator.validator.cache_key import cache_key, cache_path
if TYPE_CHECKING:
    from orchestrator.core.settings import Settings
_log = structlog.get_logger(__name__)


async def validator_self_test(settings: Settings) -> bool:
    """Confirm the cache mount is usable and key derivation runs."""
    root = Path(settings.lancache_nginx_cache_path)
    try:
        if not root.is_dir():
            _log.error("validator.self_test.cache_root_missing", path=str(root))
            return False
        next(iter(root.iterdir()), None)  # read access (listable)
        h = cache_key(settings.steam_cache_identifier, "/depot/0/chunk/" + "0" * 40,
                      "bytes=0-0")
        cache_path(root, h, settings.cache_levels)  # derivation smoke
        _log.info("validator.self_test.ok", path=str(root))
        return True
    except OSError as e:
        _log.error("validator.self_test.failed", reason=str(e)[:200])
        return False
```

- [ ] **Step 4: Wire into lifespan** in `main.py` (after settings are available, alongside the other subsystem init):
```python
    from orchestrator.validator.self_test import validator_self_test
    app.state.validator_healthy = await validator_self_test(settings)
```

- [ ] **Step 5: Update `health.py`** — replace the stub. Read the flag and include it in the conjunction:
```python
    validator_healthy = getattr(request.app.state, "validator_healthy", False)
    ...
    all_healthy = (pool_ok and scheduler_running and lancache_reachable
                   and cache_volume_mounted and validator_healthy)
```
(Keep `validator_healthy` in the `HealthResponse` body — it already exists.)

- [ ] **Step 6: Run — expect PASS**

Run: `.venv/bin/pytest tests/api/test_health_endpoint.py tests/api/test_lifespan.py tests/validator/test_self_test.py -q`

---

## Task 9: Full gate sweep + docs + combined commit

- [ ] **Step 1: Full suite**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q` — expect all green (~920+).

- [ ] **Step 2: Lint/format/type/secrets**

```
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
gitleaks detect --no-banner --no-git
semgrep --config=auto --quiet src/orchestrator/validator src/orchestrator/jobs/handlers/validate.py src/orchestrator/api/routers/validate_trigger.py
```

- [ ] **Step 3: Security audit doc** — `docs/security-audits/f7-cache-validator-security-audit.md` (focus: path traversal via depot_id/sha, worker-confined deserialization, no new auth surface). Then `scripts/process-checklist.sh --complete-step build_loop:security_audit`.

- [ ] **Step 4: Docs** — CHANGELOG (Added F7 + Data Model migration 0003), FEATURES.md (Feature 17), then `--complete-step build_loop:documentation_updated`.

- [ ] **Step 5: Mark remaining build-loop steps** (tests_written, tests_verified_failing, implemented before audit; feature_recorded after docs) and `scripts/test-gate.sh --record-feature "F7-cache-validator"`.

- [ ] **Step 6: Confirm A/B/C commit structure with the Orchestrator, then the combined `feat(f7)` commit; push; open PR.**

---

## Self-Review

**Spec coverage:** §1 in-scope items all mapped — validate handler (T6), trigger (T7), cache-key (T3), worker expand (T4), batched stat (T5), validation_history+status (T6), self-test+health (T8), depot_id migration (T1). Settings D9 (T2). Out-of-scope items not planned. ✓

**Placeholder scan:** Test bodies in T5/T6 marked with `...` are intentionally schematic for the per-outcome cases — the executing agent fills them using the concrete cached/missing tmp-tree pattern shown in T5 Step 1 and the golden-vector path derivation; all *implementation* code blocks are complete. Acceptable for inline execution by the author.

**Type consistency:** `ValidationResult` fields (T5) match the INSERT binding order (T6). `manifest_expand` returns `{depot_id, chunk_shas}` (T4) consumed identically in T5. `cache_path(root, h, levels)` signature consistent across T3/T5/T8. `validator_healthy` flag name consistent T8 lifespan↔health. ✓
