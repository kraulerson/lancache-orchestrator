# Delete the Legacy ValvePython Steam Worker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the entire ValvePython/steam gevent-subprocess worker; the orchestrator becomes a pure consumer of SteamPrefill (validate sourced from SteamPrefill's manifest cache, parsed by the agent in dependency-free Python; library enumeration from SteamPrefill; auth already SteamPrefill's).

**Architecture:** Three dependent phases, each its own PR + flag + operator-collaborative live flip, validate live throughout. ③a: agent `POST /v1/steam/validate` (parse SteamPrefill `.bin` → cache-key → stat) behind `steam_validate_via_agent`. ③b: `SteamPrefillDriver.list_owned` behind `steam_enumerate_via_prefill`. ③c: delete the worker stack + manifest_fetch + Steam auth flow + venv; collapse flags; drop `manifests.raw`.

**Tech Stack:** Python 3.12, FastAPI, httpx, pydantic, pytest, ruff, mypy. The new manifest parser uses **no third-party library** (raw protobuf walk). Spec: `docs/superpowers/specs/2026-06-21-steam-worker-deletion-design.md`. Format reference: the spike (`reference_steamprefill_manifest_format`).

---

## Conventions (apply to every task)

- Tests: `.venv/bin/python -m pytest <path> -v`. Full suite: `.venv/bin/python -m pytest -q --ignore=tests/scripts` (the `tests/scripts` dir hangs on a pre-existing `check-phase-gate.sh` bug; the only acceptable failure is `tests/test_licenses.py` — pip-licenses not on PATH).
- **Before EVERY commit:** `.venv/bin/mypy src/orchestrator` (must be clean) AND `.venv/bin/ruff format src tests` + `.venv/bin/ruff check src tests`. Bare `dict` return annotations need `dict[str, Any]`. NO `assert` in `src/` (ruff S101) — narrow with `if x is None: raise RuntimeError(...)`.
- `enforce-plan-tracking`: mark the plan's task in_progress (TaskUpdate) before its source edits.
- `enforce-evaluate`: present an evaluation + run `bash .claude/framework/hooks/mark-evaluated.sh "<desc>"` before EACH commit.
- NO per-task commits. ONE `feat` commit at the END of each phase (present A/B/C first). The operator pushes; Karl merges.
- The **live flips are operator-collaborative** (the controller runs them on the box, NOT a subagent) and GATE the next phase.

## Key resolved design decisions

- **Manifest cache location:** the agent reads SteamPrefill's manifest cache from a new read-only mount of the host's `/root/.cache/SteamPrefill` (Karl's cron-prefilled library, 2117 `.bin`s; world-readable dirs/files). A new agent setting `steam_manifest_cache_dir: Path = Path("/steamprefill-cache")` points at the mounted path; the deploy mounts `-v /root/.cache/SteamPrefill:/steamprefill-cache:ro`. `successfullyDownloadedDepots.json` stays at `/SteamPrefill/Config` (already mounted) — both reflect the cron's state, so they're consistent.
- **`/v1/steam/validate` is SYNCHRONOUS** (like `/v1/stat`): `validate_chunks` already offloads stat to the bounded executor in batches; even a 163k-chunk app stats in a few seconds. No async job registry needed.
- **Parser fixture:** `tests/agent/fixtures/sample_manifest.bin` = a copy of the box's `1018130_1018130_1018131_2926834372583665729.bin` (2913 bytes, depot 1018131, **60 chunk SHAs**). Grab it with:
  `scp` is not possible from a subagent; the controller stages it once (see Task 1, Step 0).

---

# PHASE ③a — Agent Steam-validate (gating)

## Task 1: Commit the parser fixture + the manifest parser module

**Files:**
- Create: `tests/agent/fixtures/sample_manifest.bin` (binary, staged by controller)
- Create: `src/orchestrator/agent/manifest_parser.py`
- Test: `tests/agent/test_manifest_parser.py`

- [ ] **Step 0 (controller, not subagent): stage the fixture.** On the box copy a small manifest out and into the repo:
  ```bash
  ssh karl@192.168.1.40 'docker run --rm -v /root/.cache/SteamPrefill:/c:ro alpine cat /c/v1/1018130_1018130_1018131_2926834372583665729.bin' > "tests/agent/fixtures/sample_manifest.bin"
  ```
  Verify size 2913 bytes. (Manifests are public depot chunk lists — no secrets.)

- [ ] **Step 1: Write the failing test** — `tests/agent/test_manifest_parser.py`:

```python
"""Tests for the SteamPrefill manifest (.bin) chunk-SHA parser."""

from __future__ import annotations

from pathlib import Path

from orchestrator.agent.manifest_parser import parse_chunk_shas

FIXTURE = Path(__file__).parent / "fixtures" / "sample_manifest.bin"


def test_parses_expected_chunk_count():
    shas = parse_chunk_shas(FIXTURE.read_bytes())
    assert len(shas) == 60


def test_chunk_shas_are_40_lowercase_hex():
    shas = parse_chunk_shas(FIXTURE.read_bytes())
    for s in shas:
        assert len(s) == 40
        assert s == s.lower()
        int(s, 16)  # parses as hex


def test_known_sha_present():
    shas = parse_chunk_shas(FIXTURE.read_bytes())
    assert "05c4fb5c153fc90fb89a05689fcf9edc494c1323" in shas


def test_dedups_across_files():
    # parse_chunk_shas returns a set, so a chunk shared by multiple files
    # appears once; the fixture's 60 is already the unique count.
    shas = parse_chunk_shas(FIXTURE.read_bytes())
    assert isinstance(shas, set)


def test_malformed_returns_empty_not_crash():
    assert parse_chunk_shas(b"\x00\x01\x02not-a-manifest") == set()
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError: orchestrator.agent.manifest_parser`).
  Run: `.venv/bin/python -m pytest tests/agent/test_manifest_parser.py -v`

- [ ] **Step 3: Implement** — `src/orchestrator/agent/manifest_parser.py`:

```python
"""Parse a SteamPrefill cached manifest (.bin) → set of chunk SHA1s.

The .bin is protobuf-net of SteamPrefill's `Manifest`:
  Manifest:  [1] repeated FileData,  [2] manifest gid,  [4] depot id
  FileData:  [1] repeated ChunkData
  ChunkData: [1] ChunkId (lowercase-hex SHA1 string), [2] compressed length
So chunk SHAs = field-1 (FileData) -> field-1 (ChunkData) -> field-1 (hex string),
deduped. Proven byte-identical to ValvePython on 4 depots (spike 2026-06-21).
Pure stdlib — no protobuf library, no ValvePython, no gevent.
"""

from __future__ import annotations


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    val = shift = 0
    while True:
        x = b[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not x & 0x80:
            break
        shift += 7
    return val, i


def _length_delimited_fields(b: bytes) -> list[tuple[int, bytes]]:
    """Return [(field_num, payload)] for wire-type-2 fields; skip the rest."""
    i = 0
    out: list[tuple[int, bytes]] = []
    n = len(b)
    while i < n:
        tag = b[i]
        i += 1
        field = tag >> 3
        wire = tag & 0x7
        if wire == 2:
            ln, i = _read_varint(b, i)
            out.append((field, b[i : i + ln]))
            i += ln
        elif wire == 0:
            _, i = _read_varint(b, i)
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:  # unknown wire type — stop walking this level
            break
    return out


def parse_chunk_shas(data: bytes) -> set[str]:
    """Extract the deduped set of chunk SHA1 hex strings from a .bin. A
    malformed/unrecognized buffer yields an empty set (never raises)."""
    shas: set[str] = set()
    try:
        for f, filedata in _length_delimited_fields(data):  # Manifest.Files
            if f != 1:
                continue
            for cf, chunkdata in _length_delimited_fields(filedata):  # FileData.Chunks
                if cf != 1:
                    continue
                for idf, val in _length_delimited_fields(chunkdata):  # ChunkData.ChunkId
                    if idf == 1:
                        shas.add(val.decode("ascii", "replace"))
    except (IndexError, ValueError):
        return set()
    return shas
```

- [ ] **Step 4: Run → PASS** (5 tests). Then `.venv/bin/ruff check` + `.venv/bin/mypy src/orchestrator/agent/manifest_parser.py` clean.

Note: `_length_delimited_fields` can `IndexError` on a truncated buffer — the `try/except` in `parse_chunk_shas` turns that into an empty set (the malformed test).

---

## Task 2: Agent manifest locator (find current per-depot .bin for an app)

**Files:**
- Create: `src/orchestrator/agent/manifest_locator.py`
- Test: `tests/agent/test_manifest_locator.py`

The locator maps an `app_id` → list of `.bin` paths to parse, using `successfullyDownloadedDepots.json` (the gids SteamPrefill prefilled) cross-referenced with the cache filenames `{app}_{app}_{depot}_{gid}.bin`.

- [ ] **Step 1: Write the failing test** — `tests/agent/test_manifest_locator.py`:

```python
"""Tests for locating an app's current manifest .bin files."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.agent.manifest_locator import locate_manifest_bins


def _setup(tmp_path: Path, downloaded: dict, bin_names: list[str]) -> tuple[Path, Path]:
    cache = tmp_path / "cache" / "v1"
    cache.mkdir(parents=True)
    for name in bin_names:
        (cache / name).write_bytes(b"x")
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text(json.dumps(downloaded))
    return cache.parent, cfg


def test_locates_bins_for_app(tmp_path):
    cache_root, cfg = _setup(
        tmp_path,
        {"440": [111, 222]},
        ["440_440_4401_111.bin", "440_440_4402_222.bin", "570_570_5701_999.bin"],
    )
    found = locate_manifest_bins(440, cache_root=cache_root, config_dir=cfg)
    names = sorted(p.name for p in found)
    assert names == ["440_440_4401_111.bin", "440_440_4402_222.bin"]


def test_app_not_prefilled_returns_empty(tmp_path):
    cache_root, cfg = _setup(tmp_path, {"440": [111]}, ["440_440_4401_111.bin"])
    assert locate_manifest_bins(999, cache_root=cache_root, config_dir=cfg) == []


def test_missing_bin_for_gid_skipped(tmp_path):
    # gid 222's .bin is absent on disk -> only the present one is returned.
    cache_root, cfg = _setup(tmp_path, {"440": [111, 222]}, ["440_440_4401_111.bin"])
    found = locate_manifest_bins(440, cache_root=cache_root, config_dir=cfg)
    assert [p.name for p in found] == ["440_440_4401_111.bin"]


def test_no_downloaded_file_returns_empty(tmp_path):
    cache = tmp_path / "cache" / "v1"
    cache.mkdir(parents=True)
    cfg = tmp_path / "Config"
    cfg.mkdir()
    assert locate_manifest_bins(440, cache_root=cache.parent, config_dir=cfg) == []
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** — `src/orchestrator/agent/manifest_locator.py`:

```python
"""Locate an app's current manifest .bin files in SteamPrefill's cache.

SteamPrefill records what it prefilled in Config/successfullyDownloadedDepots.json
({app_id_str: [manifest_gid_ints]}) and caches each manifest as
<cache_root>/v1/{app}_{app}_{depot}_{gid}.bin. We pick the .bin for each gid the
app prefilled (the current per-depot manifests).
"""

from __future__ import annotations

import json
from pathlib import Path


def locate_manifest_bins(app_id: int, *, cache_root: Path, config_dir: Path) -> list[Path]:
    downloaded_path = config_dir / "successfullyDownloadedDepots.json"
    if not downloaded_path.exists():
        return []
    try:
        downloaded = json.loads(downloaded_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    gids = downloaded.get(str(app_id))
    if not gids:
        return []
    v1 = cache_root / "v1"
    found: list[Path] = []
    for gid in gids:
        # filename is {app}_{app}_{depot}_{gid}.bin; depot is unknown here, glob by gid.
        matches = list(v1.glob(f"{app_id}_{app_id}_*_{gid}.bin"))
        found.extend(matches)
    return found
```

- [ ] **Step 4: Run → PASS** (4 tests). `ruff` + `mypy` clean.

---

## Task 3: Agent `POST /v1/steam/validate` endpoint

**Files:**
- Modify: `src/orchestrator/agent/routers/steam.py`
- Test: `tests/agent/test_steam_validate.py`

The endpoint ties together: locate → parse → cache-key → `validate_chunks`. Reuse `validator/cache_key.py` (`steam_chunk_uri`, `cache_key`, `cache_path`, `slice_range_zero`) and `validator/disk_stat.py` `validate_chunks`. Read cache config from `app.state.settings` (`steam_cache_identifier`, `cache_slice_size_bytes`, `cache_levels`, `lancache_nginx_cache_path`) + the new `steam_manifest_cache_dir`.

- [ ] **Step 1: Write the failing test** — `tests/agent/test_steam_validate.py`:

```python
"""Tests for the agent POST /v1/steam/validate endpoint."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.agent.manifest_parser import parse_chunk_shas
from orchestrator.core.settings import Settings
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_manifest.bin"
TOKEN = "a" * 32
APP, DEPOT, GID = 1018130, 1018131, 2926834372583665729


def _build(tmp_path: Path, *, cache_all: bool) -> TestClient:
    # SteamPrefill manifest cache (the .bin) + downloaded record.
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    (mcache / "v1" / f"{APP}_{APP}_{DEPOT}_{GID}.bin").write_bytes(FIXTURE.read_bytes())
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text(json.dumps({str(APP): [GID]}))

    # lancache cache dir — optionally create every chunk file so all are "cached".
    cache_root = tmp_path / "lancache"
    levels, ident, slice_sz = "2:2", "steam", 10_485_760
    if cache_all:
        slice_range = slice_range_zero(slice_sz)
        for sha in parse_chunk_shas(FIXTURE.read_bytes()):
            h = cache_key(ident, steam_chunk_uri(DEPOT, sha), slice_range)
            p = cache_path(cache_root, h, levels)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"data")
    else:
        cache_root.mkdir()

    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=cache_root,
        cache_levels=levels,
        steam_cache_identifier=ident,
        cache_slice_size_bytes=slice_sz,
        steam_manifest_cache_dir=mcache,
        steam_prefill_config_dir=cfg,
    )
    app = create_agent_app(settings=settings)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return client


def test_validate_all_cached(tmp_path):
    client = _build(tmp_path, cache_all=True)
    r = client.post("/v1/steam/validate", json={"app_id": APP})
    assert r.status_code == 200
    body = r.json()
    assert body["chunks_total"] == 60
    assert body["chunks_cached"] == 60
    assert body["chunks_missing"] == 0
    assert body["outcome"] == "cached"


def test_validate_all_missing(tmp_path):
    client = _build(tmp_path, cache_all=False)
    body = client.post("/v1/steam/validate", json={"app_id": APP}).json()
    assert body["chunks_total"] == 60
    assert body["chunks_cached"] == 0
    assert body["outcome"] == "missing"


def test_validate_no_manifest(tmp_path):
    client = _build(tmp_path, cache_all=False)
    body = client.post("/v1/steam/validate", json={"app_id": 999999}).json()
    assert body["chunks_total"] == 0
    assert body["outcome"] == "error"
    assert "no_manifest" in body["error"]


def test_validate_bad_app_id(tmp_path):
    client = _build(tmp_path, cache_all=False)
    r = client.post("/v1/steam/validate", json={"app_id": -5})
    assert r.status_code == 422
```

- [ ] **Step 2: Run → FAIL** (404 — route absent; also `Settings` rejects `steam_manifest_cache_dir` until Task 4). **Do Task 4 (settings) first if `Settings(...)` errors here**, then return. (Order note: implement Task 4's settings field before this test can construct `Settings`.)

- [ ] **Step 3: Implement.** Add to `src/orchestrator/agent/routers/steam.py` (it already imports `APIRouter, HTTPException, Request, status`, `BaseModel, ConfigDict, Field`, `Any`):

```python
# add imports at top
from pathlib import Path

from orchestrator.agent.manifest_locator import locate_manifest_bins
from orchestrator.agent.manifest_parser import parse_chunk_shas
from orchestrator.validator.cache_key import (
    cache_key,
    cache_path,
    slice_range_zero,
    steam_chunk_uri,
)
from orchestrator.validator.disk_stat import validate_chunks


class SteamValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: int = Field(..., ge=0)


def _classify(total: int, cached: int) -> str:
    if total == 0:
        return "cached"
    if cached == total:
        return "cached"
    if cached == 0:
        return "missing"
    return "partial"


@router.post("/v1/steam/validate")
async def steam_validate(body: SteamValidateRequest, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    cache_root = Path(settings.lancache_nginx_cache_path)
    config_dir = Path(settings.steam_prefill_config_dir)
    manifest_cache = Path(settings.steam_manifest_cache_dir)

    bins = locate_manifest_bins(body.app_id, cache_root=manifest_cache, config_dir=config_dir)
    if not bins:
        return {
            "chunks_total": 0,
            "chunks_cached": 0,
            "chunks_missing": 0,
            "outcome": "error",
            "versions": "",
            "error": "no_manifest_in_cache",
        }

    slice_range = slice_range_zero(settings.cache_slice_size_bytes)
    identifier = settings.steam_cache_identifier
    levels = settings.cache_levels

    seen: set[tuple[int, str]] = set()
    paths = []
    versions = []
    for binpath in bins:
        # filename: {app}_{app}_{depot}_{gid}.bin
        parts = binpath.stem.split("_")
        depot_id = int(parts[2])
        gid = parts[3]
        versions.append(f"{depot_id}:{gid}")
        for sha in parse_chunk_shas(binpath.read_bytes()):
            key = (depot_id, sha)
            if key in seen:
                continue
            seen.add(key)
            uri = steam_chunk_uri(depot_id, sha)
            h = cache_key(identifier, uri, slice_range)
            paths.append(cache_path(cache_root, h, levels))

    cached, missing = await validate_chunks(paths)
    total = len(paths)
    return {
        "chunks_total": total,
        "chunks_cached": cached,
        "chunks_missing": missing,
        "outcome": _classify(total, cached),
        "versions": ",".join(sorted(versions)),
        "error": None,
    }
```

- [ ] **Step 4: Run → PASS** (4 tests). Full agent dir green; `ruff` + `mypy` clean.

---

## Task 4: Settings — `steam_manifest_cache_dir` + `steam_validate_via_agent`

**Files:**
- Modify: `src/orchestrator/core/settings.py`
- Test: `tests/core/test_settings.py`

- [ ] **Step 1: Write the failing test** — append to `tests/core/test_settings.py`:

```python
class TestSteamWorkerDeletionSettings:
    def test_defaults(self):
        s = Settings(orchestrator_token="a" * 32)
        assert s.steam_manifest_cache_dir == Path("/steamprefill-cache")
        assert s.steam_validate_via_agent is False

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ORCH_STEAM_VALIDATE_VIA_AGENT", "true")
        s = Settings(orchestrator_token="a" * 32)
        assert s.steam_validate_via_agent is True
```

(`Path` is imported in that test file; if not, add `from pathlib import Path`.)

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement.** In `src/orchestrator/core/settings.py`, after the data-plane agent block (the `agent_bind_port` field), add:

```python
    # --- Steam worker deletion (re-arch step 3) ---------------------
    # The agent reads SteamPrefill's manifest cache (mounted read-only from the
    # host's /root/.cache/SteamPrefill) to source chunk SHAs for validate.
    steam_manifest_cache_dir: Path = Path("/steamprefill-cache")
    # Flag: route Steam validate through the agent's /v1/steam/validate (parses
    # SteamPrefill manifests) instead of the legacy worker manifest_expand.
    steam_validate_via_agent: bool = False
```

- [ ] **Step 4: Run → PASS.** `ruff` + `mypy` clean.

---

## Task 5: `AgentClient.steam_validate`

**Files:**
- Modify: `src/orchestrator/clients/agent_client.py`
- Test: `tests/clients/test_agent_client.py`

- [ ] **Step 1: Write the failing test** — append to `tests/clients/test_agent_client.py`:

```python
async def test_steam_validate_single_call():
    def handler(request):
        assert request.url.path == "/v1/steam/validate"
        return httpx.Response(
            200,
            json={
                "chunks_total": 60, "chunks_cached": 60, "chunks_missing": 0,
                "outcome": "cached", "versions": "1018131:x", "error": None,
            },
        )

    client = _client(handler)
    res = await client.steam_validate(1018130)
    assert res["chunks_cached"] == 60
    assert res["outcome"] == "cached"


async def test_steam_validate_unreachable_raises():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(AgentError):
        await client.steam_validate(1018130)
```

- [ ] **Step 2: Run → FAIL** (`AttributeError: steam_validate`).

- [ ] **Step 3: Implement.** Add to `AgentClient` (mirror `stat`):

```python
    async def steam_validate(self, app_id: int) -> dict[str, Any]:
        resp = await self._request("POST", "/v1/steam/validate", json={"app_id": app_id})
        result: dict[str, Any] = resp.json()
        return result
```

- [ ] **Step 4: Run → PASS.** `ruff` + `mypy` clean.

---

## Task 6: `validate_game` Steam path behind `steam_validate_via_agent`

**Files:**
- Modify: `src/orchestrator/validator/disk_stat.py` (`validate_game`)
- Test: `tests/validator/test_disk_stat.py`

When `settings.steam_validate_via_agent`, `validate_game` calls `deps.agent_client.steam_validate(app_id)` and maps the dict → `ValidationResult`, skipping the worker + DB-manifest + cache-key path entirely. Flag-off = the existing path (unchanged). The app_id comes from the game row — `validate_game` currently takes `game_id`; it must look up `app_id`. Confirm the games lookup: `validate_game` already has `pool`; add a small `SELECT app_id FROM games WHERE id=?` when the flag is on.

- [ ] **Step 1: Write the failing test** — add to `tests/validator/test_disk_stat.py` (mirror the existing `validate_game` harness; provide `deps.agent_client` with a `_FakeAgent.steam_validate`):

```python
async def test_validate_game_via_agent_steam_validate(monkeypatch, tmp_path):
    # steam_validate_via_agent=True -> validate_game calls agent.steam_validate(app_id)
    # and maps the result; the worker manifest_expand path is NOT used.
    ...
```

> The implementer copies the existing `validate_game` happy-path test harness (fake pool returning a steam game row with a known `app_id`, a `Settings(..., steam_validate_via_agent=True)`), supplies `deps.agent_client` with a `_FakeAgent` whose `steam_validate(app_id)` records the call and returns `{"chunks_total":60,"chunks_cached":55,"chunks_missing":5,"outcome":"partial","versions":"1018131:x","error":None}`, and asserts: (1) `agent.steam_validate` was called with the row's `app_id`; (2) `deps.steam_client.manifest_expand` was NOT called (monkeypatch it to raise); (3) the returned `ValidationResult` has `chunks_total=60, chunks_cached=55, chunks_missing=5, outcome="partial", manifest_version="1018131:x"`. Also assert the existing flag-off tests are unchanged + green.

- [ ] **Step 2: Run → FAIL** (`-k via_agent`).

- [ ] **Step 3: Implement.** At the top of `validate_game`, before the existing cache_root/steam_client logic, add the agent branch:

```python
    if settings.steam_validate_via_agent:
        if deps.agent_client is None:
            return ValidationResult(0, 0, 0, "error", "", "agent_client unavailable")
        row = await pool.read_one("SELECT app_id FROM games WHERE id=?", (game_id,))
        if row is None:
            return ValidationResult(0, 0, 0, "error", "", f"game {game_id} not found")
        try:
            app_id_int = int(row["app_id"])
        except (TypeError, ValueError):
            return ValidationResult(0, 0, 0, "error", "", "app_id not numeric")
        res = await deps.agent_client.steam_validate(app_id_int)
        return ValidationResult(
            chunks_total=res["chunks_total"],
            chunks_cached=res["chunks_cached"],
            chunks_missing=res["chunks_missing"],
            outcome=res["outcome"],
            manifest_version=res.get("versions", ""),
            error=res.get("error"),
        )
```

(Use the exact `ValidationResult` field names from the dataclass — `manifest_version`, not `version`. Verify the constructor signature in the file.)

- [ ] **Step 4: Run → PASS** (new + all existing validate tests, flag-off unchanged). `ruff` + `mypy` clean.

---

## Task 7: Phase ③a — full verify + commit + PR

- [ ] **Step 1:** Full suite `--ignore=tests/scripts` (only `test_licenses` may fail), `ruff format` + `ruff check` + `mypy src/orchestrator` all clean.
- [ ] **Step 2:** Present A/B/C (recommend A: single `feat(steam): agent /v1/steam/validate + flag (re-arch ③a)`). Run `mark-evaluated.sh`. WAIT for the pick.
- [ ] **Step 3:** Single `feat` commit. Push `feat/steam-worker-deletion`. PR. Body notes: the deploy must add `-v /root/.cache/SteamPrefill:/steamprefill-cache:ro` to the agent; flip `ORCH_STEAM_VALIDATE_VIA_AGENT=true` after deploy; validate is unchanged with the flag off.

### ⛔ OPERATOR-COLLABORATIVE GATE A (controller, not a subagent)
After Karl merges ③a: rebuild the image; redeploy the agent **with the new `/steamprefill-cache` mount**; flip `ORCH_STEAM_VALIDATE_VIA_AGENT=true` (recreate the orchestrator — env-file change needs recreate, not restart); validate a known cached game (e.g. game 56 / app 340) and confirm the counts match the pre-flip worker baseline. Only proceed to ③b once this is green.

---

# PHASE ③b — Library enumerate via SteamPrefill

## Task 8: Recon `list_owned` source (controller, live)

- [ ] **Step 1 (controller, live):** Determine how SteamPrefill exposes the owned-app set. Investigate on the box: `/SteamPrefill/SteamPrefill select-apps --help`, any owned-apps/app-info cache file under `/SteamPrefill` or the SteamPrefill cache, and whether `select-apps` can emit a machine-readable owned list. Record the chosen source. **Fallback if none is clean:** enumerate owned apps from the keys of `successfullyDownloadedDepots.json` (covers Karl's cron-prefilled library) — sufficient for the orchestrator's `games` table.

## Task 9: `SteamPrefillDriver.list_owned`

**Files:**
- Modify: `src/orchestrator/platform/steam/prefill_driver.py`
- Test: `tests/platform/steam/test_prefill_driver.py`

- [ ] **Step 1: Write the failing test** — based on Task 8's chosen source. If the fallback (downloaded-depots keys) is used:

```python
def test_list_owned_from_downloaded_state(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text('{"440":[1],"570":[2]}')
    d = SteamPrefillDriver(binary=tmp_path / "bin", config_dir=cfg)
    owned = d.list_owned()
    assert sorted(o.app_id for o in owned) == [440, 570]
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `list_owned()` per Task 8. Fallback implementation + an `OwnedApp` dataclass:

```python
@dataclass(frozen=True)
class OwnedApp:
    app_id: int
    name: str = ""


# in SteamPrefillDriver:
    def list_owned(self) -> list[OwnedApp]:
        """Owned apps SteamPrefill knows about. Fallback source: the keys of
        successfullyDownloadedDepots.json (apps the cron has prefilled)."""
        p = self._config_dir / "successfullyDownloadedDepots.json"
        if not p.exists():
            return []
        data = json.loads(p.read_text())
        return [OwnedApp(app_id=int(k)) for k in data]
```

(If Task 8 found a richer owned-apps source with names, implement that instead and adjust the test.)

- [ ] **Step 4: Run → PASS.** `ruff` + `mypy` clean.

## Task 10: Steam `library_sync` behind `steam_enumerate_via_prefill`

**Files:**
- Modify: `src/orchestrator/core/settings.py` (add `steam_enumerate_via_prefill: bool = False`)
- Modify: `src/orchestrator/jobs/handlers/library_sync.py` (`_steam_library_sync`)
- Modify: `src/orchestrator/jobs/worker.py` (`Deps` — `prefill_driver` is already present; no change needed if the handler reads `deps.prefill_driver`)
- Test: `tests/jobs/test_library_sync*` / the existing library_sync tests

- [ ] **Step 1: Write the failing test** — flag-on path calls `deps.prefill_driver.list_owned()` and upserts `games` (same `_UPSERT_SQL`), NOT the worker. Mirror the existing steam library_sync test harness. Assert flag-off unchanged.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement.** Add the settings flag. In `_steam_library_sync`, branch:

```python
    settings = get_settings()
    if settings.steam_enumerate_via_prefill:
        if deps.prefill_driver is None:
            raise RuntimeError("prefill_driver required when steam_enumerate_via_prefill")
        owned = deps.prefill_driver.list_owned()
        apps = [{"app_id": o.app_id, "name": o.name} for o in owned]
    else:
        result = await deps.steam_client.library_enumerate()
        apps = result["apps"]
    # ... existing upsert loop over `apps` ...
```

(Adapt to the handler's existing upsert shape — confirm the `apps` item keys the upsert expects: `app_id`, `name`, and whether `depots`/`version` are required. If the worker path provided `depots`/`version` that the upsert uses, the prefill path must supply equivalents or the upsert must tolerate their absence — resolve in the task by reading the handler.)

- [ ] **Step 4: Run → PASS** (new + existing flag-off). `ruff` + `mypy` clean.

## Task 11: Phase ③b — verify + commit + PR

- [ ] Full suite + `ruff` + `mypy` clean. Present A/B/C (recommend A: `feat(steam): library enumerate via SteamPrefill + flag (re-arch ③b)`). `mark-evaluated.sh`. Single `feat` commit. Push. PR.

### ⛔ OPERATOR-COLLABORATIVE GATE B
After merge: redeploy; flip `ORCH_STEAM_ENUMERATE_VIA_PREFILL=true`; run a library_sync; confirm the `games` table populates equivalently (count + sample apps). Proceed to ③c only once green AND ③a's flag has been stable.

---

# PHASE ③c — Delete the worker stack

## Task 12: Delete worker code + rewire Deps + lifespan

**Files (delete):** `src/orchestrator/platform/steam/{worker,client,protocol,session,enumerate}.py`.
**Files (modify):** `src/orchestrator/jobs/worker.py` (drop `Deps.steam_client`), `src/orchestrator/api/main.py` (drop `SteamWorkerClient` construct/start/stop/singleton + the JobsDeps `steam_client=` kwarg), `src/orchestrator/api/routers/auth.py` (drop the singleton + `get_steam_client_dep`).

- [ ] **Step 1:** Collapse the two flags first (so nothing references the worker): in `validator/disk_stat.py` `validate_game`, make the agent path unconditional (remove the `if settings.steam_validate_via_agent` branch + the entire legacy worker manifest path); in `library_sync.py`, make the `list_owned` path unconditional (remove the `else: deps.steam_client.library_enumerate()`). Remove the two settings flags. Run the validate + library_sync tests — update any that asserted the old flag-off worker path (those become the agent/prefill path unconditionally). The equivalence tests for flag-on become the only path.
- [ ] **Step 2:** Delete the five worker files. Drop `Deps.steam_client` (and its TYPE_CHECKING import). Remove the worker lifecycle from `api/main.py`. Grep for `steam_client` / `SteamWorkerClient` / `set_steam_client_singleton` / `manifest_expand` / `library_enumerate` and remove all references. Run the suite; fix every import error.
- [ ] **Step 3:** `ruff` + `mypy` + suite green (the worker is gone; nothing imports it).

## Task 13: Delete manifest_fetch + Steam auth endpoints + CLI

**Files (delete/modify):** `src/orchestrator/jobs/handlers/manifest_fetch.py` (delete) + its registration in the handlers registry; `src/orchestrator/api/routers/manifest_trigger.py` (delete the route + its `create_app` include); the Steam parts of `src/orchestrator/api/routers/auth.py` (`auth_begin`/`auth_complete`/`auth_status` — confirm Epic auth is a SEPARATE router and untouched); `src/orchestrator/cli/commands/game.py` (the `manifest/fetch` command).

- [ ] **Step 1:** Write tests asserting the removed routes 404: `POST /api/v1/games/{id}/manifest/fetch` → 404; the Steam `auth/*` endpoints → 404. (If Epic shares the auth router, assert Epic auth still works.)
- [ ] **Step 2:** Run → the 404 tests fail (routes still present). Delete the handler/routes/CLI + registrations.
- [ ] **Step 3:** Run → PASS. Remove the `manifest_fetch` kind from the jobs handler registry; grep for `manifest_fetch` and clean up. Suite + `ruff` + `mypy` green.

## Task 14: Drop `manifests.raw` migration + slim the table

**Files:**
- Create: `src/orchestrator/db/migrations/0007_drop_manifest_raw.sql` (next migration number — verify)
- Modify: the migration CHECKSUMS list if the repo maintains one (it does — keep it consistent)
- Test: `tests/db/test_migrate*` (a migration test asserting the column is gone)

- [ ] **Step 1:** Write a migration test: after migrations, `manifests` has no `raw` column but keeps `game_id, depot_id, version, chunk_count, total_bytes`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Write the migration. SQLite STRICT-table column drop — `ALTER TABLE manifests DROP COLUMN raw;` works on the repo's SQLite (verify `sqlite3 --version` ≥ 3.35; if not, rebuild-table pattern: create `manifests_new` STRICT without `raw`, `INSERT INTO manifests_new SELECT game_id,depot_id,version,fetched_at,chunk_count,total_bytes FROM manifests`, drop old, rename, recreate indexes). Update the migration CHECKSUMS list. NOTE `manifest_fetch` (the only writer of `raw`) is already deleted, so no code writes `raw` anymore; confirm nothing reads `manifests.raw` (the worker path is gone).
- [ ] **Step 4:** Run → PASS. Suite + `ruff` + `mypy` green.

## Task 15: Delete the worker venv + requirements + settings

**Files:** `requirements-steam-worker.in`, `requirements-steam-worker.txt` (delete); `Dockerfile` (remove the `venv-steam-worker` build stage + the `COPY --from=builder /build/.venv-steam-worker ...`); `src/orchestrator/core/settings.py` (remove `steam_worker_python_path` + `steam_session_dir`); any test referencing them.

- [ ] **Step 1:** Remove the requirements files, the Dockerfile stage, the settings fields. Grep for `venv-steam-worker` / `steam_worker_python_path` / `steam_session_dir` and clean up.
- [ ] **Step 2:** `ruff` + `mypy` + suite green. (Image-builds-without-venv is verified at the operator gate; locally just ensure no references remain.)

## Task 16: Phase ③c — verify + commit + PR

- [ ] Full suite + `ruff` + `mypy` clean. Present A/B/C (recommend A: `refactor(steam)!: delete legacy ValvePython worker + manifest_fetch + auth (re-arch ③c)`). `mark-evaluated.sh`. Single commit. Push. PR. Body: lists everything deleted; notes the agent already validates + enumerates via SteamPrefill (③a/③b live); the deploy drops `venv-steam-worker` (smaller image); the `manifests.raw` migration is irreversible (manifests re-derive from SteamPrefill's cache).

### ⛔ OPERATOR-COLLABORATIVE GATE C
After merge: rebuild (confirm the image builds with NO `venv-steam-worker` + is smaller); redeploy orchestrator + agent; run migration; smoke — validate a known game, run a library_sync, run a prefill — all green; confirm no worker subprocess exists and the removed routes 404. Done: the legacy worker is gone.

---

## Self-Review

**1. Spec coverage:** §0 spike → Task 1 (parser + fixture). §1 agent validate → Tasks 1-3,6. §2 list_owned → Tasks 8-10; manifest_fetch delete → Task 13; auth delete → Task 13; manifests slim → Task 14. §3 phasing/deletion → Tasks 7,11,12-16 + the three operator gates. §4 error handling → Task 3 (no_manifest, bad app_id), Task 1 (malformed). §5 testing → every task is TDD + the three live gates. §6 scope → Epic untouched throughout; no in-process ValvePython; raw dropped not migrated. ✓

**2. Placeholder scan:** Tasks 6, 10 use "implementer mirrors the existing harness" for the flag-on equivalence tests (the production code is fully literal; the assertions are spelled out) — deliberate, because those tests must copy each file's existing fixtures. Task 8 is an explicit live recon (its result parameterizes Task 9). Task 14's migration has two concrete forms (DROP COLUMN vs rebuild) gated on the sqlite version. Acceptable.

**3. Type consistency:** `parse_chunk_shas(bytes)->set[str]` (Task 1) used in Tasks 2-test, 3. `locate_manifest_bins(app_id,*,cache_root,config_dir)->list[Path]` (Task 2) used in Task 3. `steam_validate` returns the `{chunks_total,chunks_cached,chunks_missing,outcome,versions,error}` dict (Task 3) consumed by `AgentClient.steam_validate` (Task 5) → mapped to `ValidationResult` (Task 6). `list_owned()->list[OwnedApp]` (Task 9) used in Task 10. `steam_manifest_cache_dir` + `steam_validate_via_agent` (Task 4) used in Tasks 3,6. `steam_enumerate_via_prefill` (Task 10) used in Task 10. ✓

---

**Plan complete.** Execution: subagent-driven for code tasks; the three operator-collaborative gates (deploy + flip + live confirm) are run by the controller, not subagents.
