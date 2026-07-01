# Epic Cache-Validation Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Epic games the same per-chunk, on-disk validation Steam's F7 provides — real `cached/partial/missing`, `validate-all` sweep coverage, and true `Partial · N%` badges — by disk-statting each Epic chunk's lancache cache-key.

**Architecture:** An agent-side Epic validator (`/v1/epic/validate`) parses the stored Epic manifest, computes each chunk's lancache cache-key (`md5(identifier + cdn_base/chunk_path + slice)` — proven live), and disk-stats `/data/cache`, counting a chunk **present if cached under any** configured Epic identifier. The control plane feeds it the DB-stored manifest + `cdn_base`; `validate_game` dispatches by platform; `validate_one_game`'s recording (validation_history + games.status) is already platform-agnostic and reused unchanged; the sweep drops its `platform='steam'` filter.

**Tech Stack:** Python 3.12, FastAPI agent, httpx, pydantic-settings, pytest/ruff/mypy. Spec: `docs/superpowers/specs/2026-07-01-epic-validation-parity-design.md`.

## Global Constraints

- **Branch:** `feat/epic-validation-parity` (off `main`; carries the spec commit).
- **Cache-key (proven live):** `md5(identifier + uri + "bytes=0-10485759")`, `uri = cdn_base + "/" + chunk_path`, `identifier ∈ settings.epic_cache_identifiers`; chunk **present if cached under ANY** identifier. Disk layout `H[-2:]/H[-4:-2]/H` under `lancache_nginx_cache_path` (default `/data/cache/cache/`).
- **No auth / no network in the validator** — it only parses local manifest bytes + stats local files. `cdn_base`/host were SSRF-validated at fetch time.
- **Import isolation:** the agent Epic router imports only agent-safe modules (`platform/epic/manifest.py`, `validator/*`, stdlib) — never `orchestrator.api.main` / `orchestrator.db.pool`. `tests/agent/test_import_isolation.py` must stay green.
- **ruff:** no `assert` in `src/` (S101 → `if x: raise`); ≤100 cols; bare dict returns `-> dict[str, Any]`.
- **Pre-commit** runs `mypy src/orchestrator` + `ruff format` — run `.venv/bin/mypy src/orchestrator` and `.venv/bin/ruff format src/orchestrator tests` and `.venv/bin/ruff check src/orchestrator tests` before each commit.
- **Full suite:** `.venv/bin/python -m pytest -q --ignore=tests/scripts` — only acceptable failure `tests/test_licenses.py`.
- **Plan tracking:** mark each task `in_progress` (TaskUpdate) before its source edits.
- **Commit approval:** present A/B/C before each commit; Karl merges PRs.
- **`_classify` (from `agent/routers/steam.py`):** `total==0 → "cached"`; `cached==total → "cached"`; `cached==0 → "missing"`; else `"partial"`.

---

## PHASE 0 — SPIKE E1 (gates the build; read-only on the boxes, no auth)

> Run on the agent host (`ssh root@192.168.1.40`, inside `orchestrator-agent`). Record the finding in this plan's "Spike findings" section. The cache-key formula is **already proven** on one chunk (`md5(identifier+uri+slice)`, identifiers `epicgames` + `egs-cloudfront-chunks.epicgamescdn.com`); E1 confirms it at manifest scale + locks the identifier set.

### Task E1: identifier set + manifest-scale cross-check

- [ ] **Complete identifier set.** Grep the lancache access log for every `[identifier]` tag on Epic chunk lines: `grep -aoE '^\[[a-z0-9._-]+\]' /lancache/lancache/logs/access.log | ...` restricted to lines matching `ChunksV|\.chunk`. Record the full set (expected: `epicgames`, `egs-cloudfront-chunks.epicgamescdn.com`). These become the `epic_cache_identifiers` default.
- [ ] **Manifest-scale cross-check.** For one real prefilled Epic game: obtain its manifest chunk list + `cdn_base` (from a fresh prefill's `EpicManifest`, or reconstruct `cdn_base` from a logged chunk URL for that game — the `/Builds/.../default` prefix). For every chunk compute `uri = cdn_base + "/" + chunk_path(chunk, version)` and, across the identifier set, `cache_path(root, cache_key(ident, uri, slice), levels)`; count how many are present on disk. Confirm a **sensible cached ratio** consistent with the game's real cache state (the Epic analog of the Steam 400/400 check). Record the ratio.
- [ ] **Slice edge case.** Confirm no chunk has `window_size > cache_slice_size_bytes` (10 MiB) — if any do, they span multiple slices and need multi-slice keys (expected: none, Epic chunks are ~1 MB). Record.
- [ ] Record findings below; they confirm (not change) Task 1's default identifier list and Task 5's single-slice assumption.

### Spike findings (recorded 2026-07-01 — live, read-only, no auth)

- **Identifier set = exactly two:** `epicgames` (62388 log lines) + `egs-cloudfront-chunks.epicgamescdn.com` (25625). Confirms Task 1's default; no third identifier appears on `ChunksV*`/`.chunk` lines.
- **Formula proven at manifest scale:** 600 deduped real Epic chunk URLs from the access log → `cache_path(root, cache_key(identifier, uri, slice_range_zero(10MiB)), levels)` → **587/600 (97.8%) present on disk**. The 13 misses are one partially-evicted game (`o-3kpjwtwqwfl2p9wdwvpad7yqz4kt6c`), not formula errors. `md5(identifier + uri + "bytes=0-10485759")` is confirmed.
- **Derivation matches:** the log's request paths **are** `chunk_path(chunk, version)` outputs (same legendary algorithm the EpicGamesLauncher uses; the F6 tests already cover `parse_manifest`→`chunk_path`), so the validator's manifest-derived paths equal what was actually cached. `uri = cdn_base + "/" + chunk_path`.
- **Slice edge:** observed chunk response sizes 218 KB–1 MB, all < the 10 MiB slice → single-slice; no `window_size > slice` chunks → no multi-slice keys. Task 5's single-slice assumption holds.
- **No plan changes required** — proceed to Phase 1 as written.

---

## PHASE 1 — BUILD (TDD)

### Task 1: Settings — `epic_cache_identifiers`

**Files:**
- Modify: `src/orchestrator/core/settings.py`
- Test: `tests/core/test_settings.py`

**Interfaces:**
- Produces: `Settings.epic_cache_identifiers: list[str]` (env `ORCH_EPIC_CACHE_IDENTIFIERS`, comma-separated), default `["epicgames", "egs-cloudfront-chunks.epicgamescdn.com"]`.

> Mirrors the existing `allowed_source_ips` pattern (`Annotated[list[str], NoDecode]` + a `mode="before"` comma-splitter). `NoDecode` is already imported for `allowed_source_ips`.

- [ ] **Step 1: Write the failing test** — append to `tests/core/test_settings.py`:

```python
def test_epic_cache_identifiers_default():
    s = Settings(orchestrator_token="a" * 32)
    assert s.epic_cache_identifiers == ["epicgames", "egs-cloudfront-chunks.epicgamescdn.com"]


def test_epic_cache_identifiers_env_comma_split(monkeypatch):
    monkeypatch.setenv("ORCH_EPIC_CACHE_IDENTIFIERS", "epicgames, foo.example.com ,")
    s = Settings(orchestrator_token="a" * 32)
    assert s.epic_cache_identifiers == ["epicgames", "foo.example.com"]
```

- [ ] **Step 2: Run to verify fail** — `.venv/bin/python -m pytest tests/core/test_settings.py::test_epic_cache_identifiers_default -v` → FAIL (`AttributeError`).
- [ ] **Step 3: Implement** — add the field near `steam_cache_identifier` (the default list uses a lambda so the mutable default is per-instance):

```python
    # lancache cache identifiers Epic content is stored under (per CDN host —
    # e.g. epicgames-download1.akamaized.net -> "epicgames"; egs-cloudfront maps
    # to its own hostname). A chunk counts present if cached under ANY of these.
    # go-live proven (2026-07-01). Comma-separated env, or a real list.
    epic_cache_identifiers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["epicgames", "egs-cloudfront-chunks.epicgamescdn.com"]
    )
```

  and the validator next to `_split_allowed_source_ips`:

```python
    @field_validator("epic_cache_identifiers", mode="before")
    @classmethod
    def _split_epic_cache_identifiers(cls, v: Any) -> Any:
        """Accept a comma-separated env string or a real list; trim + drop empties."""
        if v is None or v == "":
            return ["epicgames", "egs-cloudfront-chunks.epicgamescdn.com"]
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/core/test_settings.py -q`.
- [ ] **Step 5: mypy + ruff.**

### Task 2: Migration `0010_manifests_cdn_base.sql`

**Files:**
- Create: `src/orchestrator/db/migrations/0010_manifests_cdn_base.sql`
- Modify: `src/orchestrator/db/migrations/CHECKSUMS`
- Test: `tests/db/` (the existing migration + checksum-verification tests exercise it)

> The next number is **0010** (highest on disk is 0009). This is a simple nullable `ADD COLUMN` — SQLite supports it directly, **no table-recreate** (unlike the jobs `kind` CHECK change). The `migrate_tools regenerate-checksums` tool referenced in the CHECKSUMS header does not exist — compute the sha256 by hand.

- [ ] **Step 1: Write the migration** `0010_manifests_cdn_base.sql`:

```sql
-- 0010_manifests_cdn_base.sql
-- Persist the Epic CDN base path (e.g. /Builds/Org/{catalogId}/{buildId}/default)
-- with each stored manifest. It is stable per game version (only the signed query
-- string is short-lived, and lancache strips it) and is required to compute the
-- Epic lancache cache-key (md5(identifier + cdn_base/chunk_path + slice)) at
-- validate time. Nullable: pre-existing Epic manifests get cdn_base=NULL and are
-- unvalidatable until re-prefilled (the nightly prefill backfills it). Steam
-- manifests leave it NULL (unused). Simple ADD COLUMN — no table recreate.
ALTER TABLE manifests ADD COLUMN cdn_base TEXT;
```

- [ ] **Step 2: Regenerate the CHECKSUMS line** — compute + append:

```bash
SHA=$(python3 -c "import hashlib,sys; print(hashlib.sha256(open('src/orchestrator/db/migrations/0010_manifests_cdn_base.sql','rb').read()).hexdigest())")
printf '0010  %s  0010_manifests_cdn_base.sql\n' "$SHA" >> src/orchestrator/db/migrations/CHECKSUMS
```

- [ ] **Step 3: Verify it applies + checksum-verifies** — `.venv/bin/python -m pytest tests/db -q` → PASS (the runner applies 0001..0010 on a fresh DB and `_verify_checksum_manifest` checks the sha). If a test asserts the migration count/latest id, update it to 10.
- [ ] **Step 4: Add a focused schema test** (in `tests/db/`, matching the dir's style) asserting `manifests` has a `cdn_base` column after migration:

```python
def test_manifests_has_cdn_base_column(migrated_conn):
    cols = [r[1] for r in migrated_conn.execute("PRAGMA table_info(manifests)").fetchall()]
    assert "cdn_base" in cols
```

  _(Match the actual migrated-DB fixture name used in `tests/db/`.)_

- [ ] **Step 5: mypy + ruff** (no src change here).

### Task 3: Prefill stores `cdn_base`

**Files:**
- Modify: `src/orchestrator/jobs/handlers/prefill.py`
- Test: `tests/jobs/handlers/test_prefill.py` (or wherever the Epic prefill test lives)

**Interfaces:**
- Consumes: `EpicManifest.cdn_base` (set by `fetch_manifest`), migration 0010's `cdn_base` column.

- [ ] **Step 1: Write the failing test** — assert a prefilled Epic manifest row has `cdn_base` populated. Find the existing Epic-prefill handler test; add (matching its fixtures) an assertion that after the Epic branch runs, `SELECT cdn_base FROM manifests WHERE game_id=?` equals the manifest's `cdn_base`. If the Epic manifest test-double lacks `cdn_base`, set it on the fake manifest.
- [ ] **Step 2: Run to verify fail** (the current INSERT doesn't write `cdn_base`).
- [ ] **Step 3: Implement** — update `_EPIC_MANIFEST_UPSERT` and its bind:

```python
_EPIC_MANIFEST_UPSERT = (
    "INSERT INTO manifests (game_id, depot_id, version, fetched_at, chunk_count, total_bytes, raw, cdn_base) "
    "VALUES (?, NULL, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?) "
    "ON CONFLICT(game_id, version) DO UPDATE SET "
    "  fetched_at = CURRENT_TIMESTAMP, "
    "  chunk_count = excluded.chunk_count, "
    "  total_bytes = excluded.total_bytes, "
    "  raw = excluded.raw, "
    "  cdn_base = excluded.cdn_base"
)
```

  and the execute bind (add `manifest.cdn_base` as the last param):

```python
    await deps.pool.execute_write(
        _EPIC_MANIFEST_UPSERT,
        (game_id, str(manifest.version), len(manifest.chunks), total_bytes, manifest.raw, manifest.cdn_base),
    )
```

- [ ] **Step 4: Run to verify pass.**  **Step 5: mypy + ruff.**

### Task 4: `validate_chunks_any` — present-if-any-candidate disk-stat

**Files:**
- Modify: `src/orchestrator/validator/disk_stat.py`
- Test: `tests/validator/test_disk_stat.py`

**Interfaces:**
- Produces: `async def validate_chunks_any(candidate_lists: list[list[Path]], *, batch_size: int = 256) -> tuple[int, int]` — returns `(cached, present)` where each item is a chunk's list of candidate paths; a chunk is `cached`/`present` if **any** candidate qualifies (same cached=size>0+owner-read, present=exists semantics as `_stat_batch`).
- Consumes: `_get_cache_stat_executor`, the same cached/present rule as `_stat_batch`.

- [ ] **Step 1: Write the failing test:**

```python
from pathlib import Path
from orchestrator.validator.disk_stat import validate_chunks_any

def _mk(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")

async def test_validate_chunks_any_counts_present_under_any_candidate(tmp_path):
    a = tmp_path / "a"; b = tmp_path / "b"; c = tmp_path / "c"
    _mk(b)  # chunk1: only its 2nd candidate exists -> cached
    # chunk2: neither candidate exists -> missing
    result = await validate_chunks_any([[a, b], [c, tmp_path / "d"]])
    assert result == (1, 1)  # (cached, present): chunk1 hits via b, chunk2 misses

async def test_validate_chunks_any_empty(tmp_path):
    assert await validate_chunks_any([]) == (0, 0)
```

- [ ] **Step 2: Run to verify fail** — function undefined.
- [ ] **Step 3: Implement** — add to `disk_stat.py` (a thread helper + the async batched wrapper):

```python
def _stat_any_batch(candidate_lists: list[list[Path]]) -> tuple[int, int, int]:
    """Per chunk, cached/present if ANY candidate qualifies. Runs in a thread."""
    cached = 0
    present = 0
    errors = 0
    for cands in candidate_lists:
        c_hit = False
        p_hit = False
        for p in cands:
            try:
                if p.is_symlink():
                    continue
                st = p.stat()
                p_hit = True
                if st.st_size > 0 and (st.st_mode & 0o400):
                    c_hit = True
                    break  # cached wins; stop checking this chunk's candidates
            except FileNotFoundError:
                pass
            except OSError:
                errors += 1
        if c_hit:
            cached += 1
        if p_hit:
            present += 1
    return cached, present, errors


async def validate_chunks_any(
    candidate_lists: list[list[Path]], *, batch_size: int = 256
) -> tuple[int, int]:
    """Return (cached, present) over chunks, each given as a list of candidate
    paths; a chunk counts if ANY candidate qualifies. For Epic, whose content is
    cached under one of several per-CDN-host identifiers. Same bounded executor +
    cached/present rule as validate_chunks_scoped."""
    loop = asyncio.get_running_loop()
    executor = _get_cache_stat_executor()
    cached = 0
    present = 0
    errors = 0
    for i in range(0, len(candidate_lists), batch_size):
        batch = candidate_lists[i : i + batch_size]
        b_cached, b_present, b_err = await loop.run_in_executor(executor, _stat_any_batch, batch)
        cached += b_cached
        present += b_present
        errors += b_err
    if errors:
        _log.warning("validate.stat_errors", error_count=errors, total=len(candidate_lists))
    return cached, present
```

- [ ] **Step 4: Run to verify pass.**  **Step 5: mypy + ruff.**

### Task 5: Agent Epic validator `POST /v1/epic/validate`

**Files:**
- Create: `src/orchestrator/agent/routers/epic.py`
- Modify: `src/orchestrator/agent/app.py` (include the router)
- Test: `tests/agent/test_epic_validate.py`

**Interfaces:**
- Produces: `POST /v1/epic/validate` (bearer-gated by the app-level middleware), body `{app_id: int, version: str, cdn_base: str, raw_manifest_b64: str}` → returns the **same dict shape as steam_validate** `{chunks_total, chunks_cached, chunks_missing, outcome, versions, error}`.
- Consumes: `platform/epic/manifest.py::{parse_manifest, chunk_path, EpicManifestError}`, `validator/cache_key.py::{epic_chunk_uri, cache_key, cache_path, slice_range_zero}`, `validator/disk_stat.py::validate_chunks_any`, `settings.epic_cache_identifiers`.

- [ ] **Step 1: Write the failing tests** (`tests/agent/test_epic_validate.py`) — build a real (tiny) Epic manifest with a couple of chunks (reuse the F6 manifest fixtures / `parse_manifest`-round-trippable bytes, or monkeypatch `parse_manifest` to return a stub `EpicManifest(version=..., chunks=[EpicChunk(...)])`), a temp cache root with a chunk file placed at the cache-key path for the **2nd** identifier only (proving present-if-any), and assert `cached==1`; plus: absent → `missing`; empty identifier list → `error`; malformed manifest → `error` (not raise); the 401-without-bearer path. Mirror `tests/agent/test_steam_validate.py`'s TestClient + settings-injection style; the settings must set `epic_cache_identifiers`, `lancache_nginx_cache_path=<tmp>`, `cache_slice_size_bytes`, `cache_levels`.
- [ ] **Step 2: Run to verify fail** — route missing.
- [ ] **Step 3: Implement** `agent/routers/epic.py`:

```python
"""Agent /v1/epic/validate — disk-stat an Epic game's stored manifest against the
lancache cache. Parity with agent/routers/steam.py::steam_validate. STDLIB + the
agent-safe validator/platform modules only; MUST NOT import orchestrator.api.* /
orchestrator.db.*. No network, no auth — parses the manifest bytes it's given and
stats local cache files."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.platform.epic.manifest import EpicManifestError, chunk_path, parse_manifest
from orchestrator.validator.cache_key import cache_key, cache_path, epic_chunk_uri, slice_range_zero
from orchestrator.validator.disk_stat import validate_chunks_any

_log = structlog.get_logger(__name__)
router = APIRouter()


class EpicValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: int = Field(..., ge=0)
    version: str
    cdn_base: str
    raw_manifest_b64: str


def _classify(total: int, cached: int) -> str:
    if total == 0 or cached == total:
        return "cached"
    if cached == 0:
        return "missing"
    return "partial"


def _err(msg: str) -> dict[str, Any]:
    return {
        "chunks_total": 0,
        "chunks_cached": 0,
        "chunks_missing": 0,
        "outcome": "error",
        "versions": "",
        "error": msg,
    }


@router.post("/v1/epic/validate", status_code=status.HTTP_200_OK)
async def epic_validate(body: EpicValidateRequest, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    identifiers = settings.epic_cache_identifiers
    if not identifiers:
        return _err("no_epic_identifiers")
    try:
        manifest = parse_manifest(base64.b64decode(body.raw_manifest_b64))
    except (EpicManifestError, ValueError) as e:
        _log.warning("epic_validate.parse_failed", app_id=body.app_id, reason=f"{type(e).__name__}: {e}"[:200])
        return _err("manifest_parse_failed")

    cache_root = Path(settings.lancache_nginx_cache_path)
    slice_range = slice_range_zero(settings.cache_slice_size_bytes)
    levels = settings.cache_levels

    candidate_lists: list[list[Path]] = []
    seen: set[str] = set()
    for chunk in manifest.chunks:
        cp = chunk_path(chunk, manifest.version)
        if cp in seen:
            continue  # de-dupe identical chunks (same content -> same path)
        seen.add(cp)
        uri = epic_chunk_uri(cp, body.cdn_base)
        candidate_lists.append(
            [cache_path(cache_root, cache_key(ident, uri, slice_range), levels) for ident in identifiers]
        )

    total = len(candidate_lists)
    if total == 0:
        return {
            "chunks_total": 0, "chunks_cached": 0, "chunks_missing": 0,
            "outcome": "cached", "versions": str(manifest.version), "error": None,
        }
    cached, _present = await validate_chunks_any(candidate_lists)
    return {
        "chunks_total": total,
        "chunks_cached": cached,
        "chunks_missing": total - cached,
        "outcome": _classify(total, cached),
        "versions": str(manifest.version),
        "error": None,
    }
```

  and in `agent/app.py` include the router next to the others (`from orchestrator.agent.routers import epic` + `app.include_router(epic.router)`).

- [ ] **Step 4: Run to verify pass** + `pytest tests/agent/test_import_isolation.py -q` green.
- [ ] **Step 5: mypy + ruff.**

### Task 6: `AgentClient.epic_validate`

**Files:**
- Modify: `src/orchestrator/clients/agent_client.py`
- Test: `tests/clients/test_agent_client.py`

**Interfaces:**
- Produces: `async def epic_validate(self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str) -> dict[str, Any]` — single POST `/v1/epic/validate` (300s timeout), returns the result dict. Mirrors `steam_validate` (direct `_request`, no poll).

- [ ] **Step 1: Write the failing test** — mirror `test_steam_validate_single_call` (the file's stub-transport `_client(handler)` pattern): assert a POST to `/v1/epic/validate` returns the result dict.
- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** — add next to `steam_validate`:

```python
    async def epic_validate(
        self, *, app_id: int, version: str, cdn_base: str, raw_manifest_b64: str
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/v1/epic/validate",
            json={
                "app_id": app_id,
                "version": version,
                "cdn_base": cdn_base,
                "raw_manifest_b64": raw_manifest_b64,
            },
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        result: dict[str, Any] = resp.json()
        return result
```

- [ ] **Step 4: Run to verify pass.**  **Step 5: mypy + ruff.**

### Task 7: Control dispatch — `validate_game` platform branch + `validate_handler` allows Epic

**Files:**
- Modify: `src/orchestrator/validator/disk_stat.py` (`validate_game` → dispatch)
- Modify: `src/orchestrator/jobs/handlers/validate.py` (`validate_handler` allow epic)
- Test: `tests/validator/test_disk_stat.py`, `tests/jobs/handlers/test_validate.py`

**Interfaces:**
- Consumes: `AgentClient.epic_validate` (Task 6), the `manifests` table with `cdn_base` (Tasks 2–3), `base64.b64encode`.
- Produces: `validate_game` returns a `ValidationResult` for both platforms; `validate_one_game`'s recording is reused unchanged.

- [ ] **Step 1: Write the failing tests:**
  - `validate_game` for an epic game with a stored manifest (`raw`, `version`, `cdn_base`) → calls a stub `agent_client.epic_validate` and shapes the result into `ValidationResult`. An epic game with NULL `cdn_base` or no manifest row → `ValidationResult(..., "error", ...)`, no agent call.
  - `validate_handler` no longer raises for `platform='epic'` (a valid epic game validates); an unknown platform still errors.

- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** — in `disk_stat.py`, make `validate_game` dispatch by platform (keep the steam path identical; add an epic branch):

```python
async def validate_game(
    pool: Any, deps: Deps, game_id: int, settings: Settings
) -> ValidationResult:
    """Validate a game's current manifest against the on-disk lancache. Steam
    delegates to /v1/steam/validate (agent-located manifest); Epic reads the
    stored manifest (+ cdn_base) from the DB and delegates to /v1/epic/validate.
    The control plane shapes the result; recording is done by validate_one_game."""
    if deps.agent_client is None:
        return ValidationResult(0, 0, 0, "error", "", "agent_client unavailable")
    row = await pool.read_one("SELECT app_id, platform FROM games WHERE id=?", (game_id,))
    if row is None:
        return ValidationResult(0, 0, 0, "error", "", f"game {game_id} not found")
    platform = row["platform"]
    if platform == "epic":
        return await _validate_epic_game(pool, deps, game_id, settings)
    try:
        app_id_int = int(row["app_id"])
    except (TypeError, ValueError):
        return ValidationResult(0, 0, 0, "error", "", "app_id not numeric")
    res = await deps.agent_client.steam_validate(app_id_int)
    return _shape(res)


def _shape(res: dict[str, Any]) -> ValidationResult:
    return ValidationResult(
        chunks_total=res["chunks_total"],
        chunks_cached=res["chunks_cached"],
        chunks_missing=res["chunks_missing"],
        outcome=res["outcome"],
        manifest_version=res.get("versions", ""),
        error=res.get("error"),
    )


async def _validate_epic_game(
    pool: Any, deps: Deps, game_id: int, settings: Settings
) -> ValidationResult:
    import base64

    row = await pool.read_one(
        "SELECT app_id, version, cdn_base, raw FROM manifests "
        "WHERE game_id=? ORDER BY fetched_at DESC LIMIT 1",
        (game_id,),
    )
    if row is None:
        return ValidationResult(0, 0, 0, "error", "", "no_manifest")
    if not row["cdn_base"]:
        return ValidationResult(0, 0, 0, "error", "", "no_cdn_base")  # pre-migration; re-prefill heals
    game = await pool.read_one("SELECT app_id FROM games WHERE id=?", (game_id,))
    app_id_int = int(game["app_id"]) if game and str(game["app_id"]).isdigit() else 0
    res = await deps.agent_client.epic_validate(
        app_id=app_id_int,
        version=str(row["version"]),
        cdn_base=str(row["cdn_base"]),
        raw_manifest_b64=base64.b64encode(row["raw"]).decode("ascii"),
    )
    return _shape(res)
```

  (Move the existing steam-shaping into `_shape` and reuse it — DRY.) In `validate.py::validate_handler`, replace the `platform != "steam"` hard-raise so epic is allowed:

```python
    platform = job.get("platform")
    if platform not in ("steam", "epic"):
        raise ValueError(f"validate supports steam+epic (got {platform!r})")
    game_id = job.get("game_id")
    if game_id is None:
        raise ValueError("validate job has no game_id")
    # validate_one_game -> validate_game dispatches by the game's real platform.
```

  (Drop the now-redundant `game["platform"] != "steam"` re-check below it; `validate_game` reads the platform itself.)

- [ ] **Step 4: Run to verify pass** (both test files) + full agent/validator/jobs test dirs green.
- [ ] **Step 5: mypy + ruff.**

### Task 8: Sweep — un-scope to Epic

**Files:**
- Modify: `src/orchestrator/jobs/handlers/sweep.py`
- Test: `tests/jobs/handlers/test_sweep.py`

- [ ] **Step 1: Write the failing test** — a sweep (full and status-gated) over a DB with both a steam and an epic game validates BOTH (assert the epic game got a `validation_history` row / its `validate_one_game` was called). Match the existing sweep test fixtures.
- [ ] **Step 2: Run to verify fail** (epic excluded).
- [ ] **Step 3: Implement** — drop `platform='steam'` from both SQLs:

```python
_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE status IN ('up_to_date','validation_failed') "
    "ORDER BY id"
)

_CANDIDATE_SQL_FULL = "SELECT id, status FROM games ORDER BY id"
```

  (Per-game dispatch is unchanged — `validate_one_game` → `validate_game` now handles epic.)

- [ ] **Step 4: Run to verify pass.**  **Step 5: mypy + ruff + full suite.**

### Task 9: Docs + security audit + single feature commit

**Files:**
- Create: `docs/security-audits/epic-validation-parity-security-audit.md`
- Modify: `CHANGELOG.md`, this plan (record spike findings)

- [ ] **Step 1: Security audit** (Senior Security Engineer persona): the validator does **no network/auth**; input is `cdn_base`/`version`/`raw_manifest_b64` from the control plane (from the DB, originally SSRF/traversal-validated at fetch time by `platform/epic/manifest.py`); `parse_manifest` has decompression-bomb + chunk-count caps; cache-key paths are `md5`-hex + fixed levels (no traversal from attacker-controlled path components — `epic_chunk_uri` joins validated `cdn_base` + computed `chunk_path`); per-chunk stat isolation; import-isolation preserved. Conclude findings.
- [ ] **Step 2: CHANGELOG** — Added: Epic disk-stat validation parity (agent `/v1/epic/validate`, `cdn_base` persistence migration 0010, sweep un-scoped, `epic_cache_identifiers`; cache-key `md5(identifier + cdn_base/chunk_path + slice)` proven live; no auth).
- [ ] **Step 3: Record spike findings** in this plan's Phase 0 section.
- [ ] **Step 4: Full verification** — `.venv/bin/mypy src/orchestrator` clean; `ruff format && ruff check` clean; `.venv/bin/python -m pytest -q --ignore=tests/scripts` (only `test_licenses.py` fails).
- [ ] **Step 5: Single feature commit** (present A/B/C first; Karl merges the PR).

---

## OPERATOR GO-LIVE (post-merge; Claude runs the boxes — no auth/2FA needed)

1. Deploy the agent image (new `/v1/epic/validate` route) + the control image (migration 0010, dispatch, sweep) — rebuild + recreate both (agent `.40`, control `1105`). Migration 0010 applies on control startup.
2. Verify: the epic route is registered (401 without bearer); `manifests.cdn_base` column exists.
3. **Backfill note:** Epic manifests stored before 0010 have `cdn_base=NULL` → `error`/unchanged until the nightly Epic prefill re-populates `cdn_base`. Optionally trigger an Epic prefill of the library to backfill `cdn_base` immediately, then validate.
4. Run `orchestrator-cli cache validate-all` (now platform-agnostic) — report the **Epic before/after** status histogram (how many epic games flip to `up_to_date` / true `validation_failed` with real `Partial · N%`).

---

## Self-Review

- **Spec coverage:** Component A (agent validator) → Task 5 (+ Task 4 primitive); B (`cdn_base` persistence) → Tasks 2–3; C (un-scope validate+sweep) → Tasks 7–8 (+ Task 6 client); D (identifier setting) → Task 1; E (Game_shelf/API unchanged) → no task (verified platform-agnostic in the spec); Phase-0 spike E1 → Phase 0; go-live → final section. ✓
- **Placeholder scan:** the only `<...>` are the E1-derived identifier confirmation + the tmp paths in tests — no hand-wavy TODOs. ✓
- **Type consistency:** the result dict shape `{chunks_total, chunks_cached, chunks_missing, outcome, versions, error}` is identical across the agent route (Task 5), `agent_client.epic_validate` (Task 6), and `_shape`/`ValidationResult` (Task 7); `validate_chunks_any(candidate_lists) -> (cached, present)` matches between Task 4 and Task 5; `epic_cache_identifiers` identical across Tasks 1 and 5; the `cdn_base` column/param name identical across Tasks 2, 3, 7. ✓
