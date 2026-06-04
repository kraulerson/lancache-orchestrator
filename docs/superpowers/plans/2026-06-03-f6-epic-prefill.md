# F6 — Epic CDN Prefill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. **No per-task commits** — implement all tasks TDD-style, then Task 13 is the single gate sweep + adversarial verify + docs + one combined commit + PR.

**Goal:** Full Epic Games support — OAuth, library enumeration, manifest fetch + pure-Python parse, chunk prefill through the lancache, and sample cache-HIT verification — mirroring the Steam pipeline but in-process (no `legendary`, no gevent, no worker).

**Architecture:** New `src/orchestrator/platform/epic/` package (`models`, `manifest`, `oauth`, `library`, `client`) + `prefill/epic_downloader.py` + Epic branches in the `library_sync`/`manifest_fetch`/`prefill` handlers (dispatch on `job.platform`) + `EpicClient` threaded via `Deps.epic_client` + Epic auth/sync routers. All async httpx + stdlib `struct`/`zlib`/`base64`. Reference implementation: `spikes/spike_b_epic_prefill.py` (PASS). Spec: `docs/superpowers/specs/2026-06-03-f6-epic-prefill-design.md`.

**Tech Stack:** Python 3.12, httpx (async), pydantic-settings, structlog, aiosqlite (via pool), stdlib struct/zlib/base64. No new dependency.

**Conventions (apply to every task):** `.venv/bin/pytest`; `ruff check` AND `ruff format` (src + tests); `mypy --strict src/`; `gitleaks`; semgrep (`no-sync-sqlite` → `sqlite3` only in `test_migrate.py`; `no-f-string-sql` → never f-string SQL, params only); license test needs `.venv/bin` on PATH. NEVER log Epic access/refresh tokens (they pass through `core/logging` redaction, but don't put them in event fields). The Epic `client_id`/`client_secret` are the public `legendary` constants (not real secrets) — fine to hardcode as settings defaults. No DB migration (schema is Epic-ready). `jobs.source` CHECK = `('scheduler','cli','gameshelf','api')`.

---

## Task 0: Scaffolding

**Files:**
- Create: `src/orchestrator/platform/epic/__init__.py` (empty)
- Create: `tests/platform/epic/__init__.py` (empty)
- Create: `tests/platform/epic/conftest.py`

- [ ] **Step 1:** Create the two empty `__init__.py` files.
- [ ] **Step 2:** `tests/platform/epic/conftest.py` re-exports pool fixtures for handler-adjacent tests:

```python
"""Shared fixtures for tests/platform/epic/. Re-exports pool fixtures."""

from __future__ import annotations

from tests.db.conftest import (  # noqa: F401
    _isolated_env,
    db_path,
    pool,
)
```

- [ ] **Step 3:** Verify collection: `Run: .venv/bin/pytest tests/platform/epic/ -q` → Expected: `no tests ran` (no error).

---

## Task 1: Epic Settings

**Files:**
- Modify: `src/orchestrator/core/settings.py` (add Epic fields near the F5 prefill settings block)
- Test: `tests/core/test_settings.py`

- [ ] **Step 1: Write failing tests** (append to `TestDefaults` value table where the existing `("pool_busy_timeout_ms", 5000)` rows live, plus a focused test):

```python
def test_epic_settings_defaults(self):
    s = Settings(orchestrator_token=VALID_TOKEN)
    assert s.epic_token_url.startswith("https://account-public-service")
    assert s.epic_client_id == "34a02cf8f4414e29b15921876da36f9a"
    assert s.epic_user_agent.startswith("EpicGamesLauncher/")
    assert s.epic_session_dir == "/var/lib/orchestrator/epic_session"
    assert s.epic_manifest_label == "Live"
    assert s.epic_platform == "Windows"
```

- [ ] **Step 2:** `Run: .venv/bin/pytest tests/core/test_settings.py::TestDefaults::test_epic_settings_defaults -v` → FAIL (AttributeError).

- [ ] **Step 3: Implement** — add to `Settings` (after the F5 `prefill_chunk_max_attempts` field; `# --- Epic (F6) ---` comment):

```python
    # --- Epic (F6) ---
    epic_token_url: str = Field(
        default="https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
    )
    epic_library_url: str = Field(
        default="https://library-service.live.use1a.on.epicgames.com/library/api/public/items"
    )
    epic_manifest_url_template: str = Field(
        default=(
            "https://launcher-public-service-prod06.ol.epicgames.com"
            "/launcher/api/public/assets/v2/platform/{platform}"
            "/namespace/{namespace}/catalogItem/{catalog_item_id}"
            "/app/{app_name}/label/{label}"
        )
    )
    # Public legendary launcher client credentials (NOT secrets — these are the
    # well-known EGS launcher app credentials used by every Epic CLI client).
    epic_client_id: str = Field(default="34a02cf8f4414e29b15921876da36f9a")
    epic_client_secret: str = Field(default="daafbccc737745039dffe53d94fc76cf")
    epic_user_agent: str = Field(
        default="EpicGamesLauncher/11.0.1-14907503+++Portal+Release-Live"
    )
    epic_session_dir: str = Field(default="/var/lib/orchestrator/epic_session")
    epic_manifest_label: str = Field(default="Live")
    epic_platform: str = Field(default="Windows")
```

- [ ] **Step 4:** `Run: .venv/bin/pytest tests/core/test_settings.py -q` → PASS.

---

## Task 2: Epic data models

**Files:**
- Create: `src/orchestrator/platform/epic/models.py`
- Test: `tests/platform/epic/test_models.py`

- [ ] **Step 1: Write failing test:**

```python
from orchestrator.platform.epic.models import (
    AuthTokens, EpicChunk, EpicLibraryItem, EpicManifest,
)

def test_models_construct():
    t = AuthTokens(access_token="a", refresh_token="r", account_id="id",
                   display_name="n", expires_at="2026-06-03T00:00:00Z")
    assert t.access_token == "a"
    c = EpicChunk(guid=(1, 2, 3, 4), hash=5, sha_hash=b"x" * 20, group_num=6,
                  file_size=7, window_size=8)
    m = EpicManifest(version=22, chunks=[c], cdn_base="http://cdn/path")
    assert m.version == 22 and m.chunks[0].group_num == 6
    item = EpicLibraryItem(app_name="App", namespace="ns",
                           catalog_item_id="cat", title="T")
    assert item.app_name == "App"
```

- [ ] **Step 2:** Run → FAIL (module missing).

- [ ] **Step 3: Implement** `models.py` (frozen dataclasses):

```python
"""Epic platform data models (F6). Pure data — no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    account_id: str
    display_name: str
    expires_at: str  # ISO8601 UTC, access-token expiry


@dataclass(frozen=True)
class EpicChunk:
    guid: tuple[int, int, int, int]
    hash: int
    sha_hash: bytes
    group_num: int
    file_size: int
    window_size: int


@dataclass
class EpicManifest:
    version: int
    chunks: list[EpicChunk] = field(default_factory=list)
    cdn_base: str = ""  # CDN base path (dir of the manifest URI), no host


@dataclass(frozen=True)
class EpicLibraryItem:
    app_name: str
    namespace: str
    catalog_item_id: str
    title: str
```

- [ ] **Step 4:** Run → PASS.

---

## Task 3: Manifest binary parser + chunk paths (RISKIEST — test hard)

**Files:**
- Create: `src/orchestrator/platform/epic/manifest.py` (parser functions only this task; the async URL fetch lands in Task 5)
- Test: `tests/platform/epic/test_manifest_parse.py`

This ports `spike_b_epic_prefill.py`'s `parse_manifest`/`chunk_path`/`_get_chunk_dir`/`_read_fstring`/`_read_guid`/`_read_array` into production form: **raise `EpicManifestError` instead of `sys.exit`**, no prints, return the `EpicManifest`/paths.

- [ ] **Step 1: Write failing tests.** Build a synthetic minimal manifest with a local `_build_manifest(version, n_chunks)` helper (uncompressed body, `stored_as=0`) so the test owns the byte layout. Cover a v≥22 (ChunksV5/base64) and a legacy (<22 hex) path, plus a bad-magic error:

```python
import struct
import pytest
from orchestrator.platform.epic.manifest import (
    EpicManifestError, parse_manifest, chunk_path, _get_chunk_dir,
)

def _build_manifest(version: int, chunks: list[dict]) -> bytes:
    # Body (uncompressed) ------------------------------------------------
    body = bytearray()
    # ManifestMeta: meta_size(u32) + meta_version(u8) + feature_level(u32)
    #   + is_file_data(u8) + app_id(u32) + 4 FStrings + prereq_count(u32)=0
    meta = bytearray()
    meta += struct.pack("<B", 0)            # meta_data_version
    meta += struct.pack("<I", 17)           # feature_level
    meta += struct.pack("<B", 0)            # is_file_data
    meta += struct.pack("<I", 1234)         # app_id
    for s in ("App", "1.0", "x.exe", "cmd"):  # FStrings (utf-8, +len incl NUL)
        b = s.encode() + b"\x00"
        meta += struct.pack("<i", len(b)) + b
    meta += struct.pack("<I", 0)            # prereq_count
    meta_size = 4 + len(meta)
    body += struct.pack("<I", meta_size) + meta
    # ChunkDataList: cdl_size(u32) + cdl_version(u8) + count(u32) + columns
    n = len(chunks)
    cdl = bytearray()
    cdl += struct.pack("<B", 0)             # cdl_version
    cdl += struct.pack("<I", n)             # chunk count
    for c in chunks:                        # guids (4x u32)
        cdl += struct.pack("<IIII", *c["guid"])
    for c in chunks:                        # hashes (u64)
        cdl += struct.pack("<Q", c["hash"])
    for c in chunks:                        # sha (20 bytes)
        cdl += c["sha"]
    for c in chunks:                        # group_num (u8)
        cdl += struct.pack("<B", c["group"])
    for c in chunks:                        # window_size (u32)
        cdl += struct.pack("<I", 1048576)
    for c in chunks:                        # file_size (i64)
        cdl += struct.pack("<q", c["size"])
    cdl_size = 4 + len(cdl)
    body += struct.pack("<I", cdl_size) + cdl
    # Header ------------------------------------------------------------
    header = bytearray()
    header += struct.pack("<I", 0x44BEC00C)         # magic
    header += struct.pack("<I", 41)                 # header_size (fixed below)
    header += struct.pack("<I", len(body))          # data_size_compressed
    header += struct.pack("<I", len(body))          # data_size_uncompressed
    header += b"\x00" * 20                           # sha hash
    header += struct.pack("<B", 0)                   # stored_as (0 = raw)
    header += struct.pack("<I", version)             # version
    header[4:8] = struct.pack("<I", len(header))     # real header_size
    return bytes(header) + bytes(body)

def _chunks(n: int) -> list[dict]:
    return [
        {"guid": (i + 1, 2, 3, 4), "hash": 100 + i, "sha": bytes([i]) * 20,
         "group": i % 100, "size": 500 + i}
        for i in range(n)
    ]

def test_parse_v22_chunks_and_base64_path():
    raw = _build_manifest(22, _chunks(2))
    m = parse_manifest(raw)
    assert m.version == 22
    assert len(m.chunks) == 2
    assert m.chunks[0].guid == (1, 2, 3, 4)
    assert m.chunks[0].hash == 100
    p = chunk_path(m.chunks[0], m.version)
    assert p.startswith("ChunksV5/00/") and p.endswith(".chunk")
    assert "_" in p.rsplit("/", 1)[1]  # base64 hash_guid

def test_parse_legacy_hex_path():
    raw = _build_manifest(18, _chunks(1))
    m = parse_manifest(raw)
    assert m.version == 18
    p = chunk_path(m.chunks[0], m.version)
    # legacy: ChunksV4/<group:02d>/<hash:016X>_<guidhex>.chunk
    assert p.startswith("ChunksV4/00/")
    assert p.split("/")[-1].startswith(f"{100:016X}_")

def test_bad_magic_raises():
    with pytest.raises(EpicManifestError):
        parse_manifest(b"\x00\x00\x00\x00" + b"\x00" * 64)

def test_chunk_dir_thresholds():
    assert _get_chunk_dir(22) == "ChunksV5"
    assert _get_chunk_dir(15) == "ChunksV4"
    assert _get_chunk_dir(6) == "ChunksV3"
    assert _get_chunk_dir(3) == "ChunksV2"
    assert _get_chunk_dir(2) == "Chunks"
```

- [ ] **Step 2:** Run → FAIL (module missing).

- [ ] **Step 3: Implement** `manifest.py` (port from spike; production hardening). Full code:

```python
"""Epic binary manifest parsing + chunk-path construction (F6).

Pure functions ported from spikes/spike_b_epic_prefill.py (PASS). No I/O. Parses
just enough of the Unreal/EGS manifest to build chunk CDN paths. Raises
EpicManifestError on malformed input (never sys.exit / never silently truncates).
"""

from __future__ import annotations

import base64
import struct
import zlib
from io import BytesIO

from orchestrator.platform.epic.models import EpicChunk, EpicManifest

_MANIFEST_MAGIC = 0x44BEC00C


class EpicManifestError(Exception):
    """Malformed or unsupported Epic manifest binary."""


def _read_fstring(bio: BytesIO) -> str:
    (length,) = struct.unpack("<i", bio.read(4))
    if length == 0:
        return ""
    if length < 0:  # UTF-16
        return bio.read(abs(length) * 2).decode("utf-16-le").rstrip("\x00")
    return bio.read(length).decode("utf-8", errors="replace").rstrip("\x00")


def _read_guid(bio: BytesIO) -> tuple[int, int, int, int]:
    a, b, c, d = struct.unpack("<IIII", bio.read(16))
    return (a, b, c, d)


def _read_array(bio: BytesIO, count: int, fmt: str) -> list[int]:
    size = struct.calcsize(fmt)
    return [struct.unpack(fmt, bio.read(size))[0] for _ in range(count)]


def parse_manifest(raw: bytes) -> EpicManifest:
    """Parse an Epic binary manifest into version + chunk list."""
    try:
        return _parse(raw)
    except EpicManifestError:
        raise
    except (struct.error, zlib.error, ValueError, IndexError) as e:
        raise EpicManifestError(f"malformed Epic manifest: {type(e).__name__}: {e}") from e


def _parse(raw: bytes) -> EpicManifest:
    bio = BytesIO(raw)
    (magic,) = struct.unpack("<I", bio.read(4))
    if magic != _MANIFEST_MAGIC:
        raise EpicManifestError(f"bad manifest magic: {magic:#010x}")
    (header_size,) = struct.unpack("<I", bio.read(4))
    bio.read(4)  # data_size_compressed
    bio.read(4)  # data_size_uncompressed
    bio.read(20)  # sha hash
    (stored_as,) = struct.unpack("<B", bio.read(1))
    (version,) = struct.unpack("<I", bio.read(4))

    bio.seek(header_size)
    body_raw = bio.read()
    body = zlib.decompress(body_raw) if (stored_as & 0x01) else body_raw
    bb = BytesIO(body)

    # ManifestMeta — read meta_size then skip to its end.
    (meta_size,) = struct.unpack("<I", bb.read(4))
    bb.read(1)  # meta_data_version
    bb.read(4)  # feature_level
    bb.read(1)  # is_file_data
    bb.read(4)  # app_id
    for _ in range(4):  # app_name, build_version, launch_exe, launch_cmd
        _read_fstring(bb)
    (prereq_count,) = struct.unpack("<I", bb.read(4))
    for _ in range(prereq_count * 4):
        _read_fstring(bb)
    bb.seek(meta_size)

    # ChunkDataList
    bb.read(4)  # cdl_size
    bb.read(1)  # cdl_version
    (chunk_count,) = struct.unpack("<I", bb.read(4))
    if chunk_count < 0 or chunk_count > 5_000_000:
        raise EpicManifestError(f"implausible chunk_count {chunk_count}")

    guids = [_read_guid(bb) for _ in range(chunk_count)]
    hashes = _read_array(bb, chunk_count, "<Q")
    sha_hashes = [bb.read(20) for _ in range(chunk_count)]
    group_nums = _read_array(bb, chunk_count, "<B")
    window_sizes = _read_array(bb, chunk_count, "<I")
    file_sizes = _read_array(bb, chunk_count, "<q")

    chunks = [
        EpicChunk(
            guid=guids[i], hash=hashes[i], sha_hash=sha_hashes[i],
            group_num=group_nums[i], file_size=file_sizes[i], window_size=window_sizes[i],
        )
        for i in range(chunk_count)
    ]
    return EpicManifest(version=version, chunks=chunks)


def _get_chunk_dir(version: int) -> str:
    if version >= 22:
        return "ChunksV5"
    if version >= 15:
        return "ChunksV4"
    if version >= 6:
        return "ChunksV3"
    if version >= 3:
        return "ChunksV2"
    return "Chunks"


def _guid_hex(guid: tuple[int, int, int, int]) -> str:
    return "".join(f"{g:08X}" for g in guid)


def chunk_path(chunk: EpicChunk, manifest_version: int) -> str:
    """CDN-relative chunk path (matches legendary). v>=22 uses base64 names."""
    chunk_dir = _get_chunk_dir(manifest_version)
    if manifest_version >= 22:
        h64 = base64.urlsafe_b64encode(struct.pack("<Q", chunk.hash)).rstrip(b"=").decode()
        g64 = base64.urlsafe_b64encode(struct.pack("<IIII", *chunk.guid)).rstrip(b"=").decode()
        return f"{chunk_dir}/{chunk.group_num:02d}/{h64}_{g64}.chunk"
    return f"{chunk_dir}/{chunk.group_num:02d}/{chunk.hash:016X}_{_guid_hex(chunk.guid)}.chunk"
```

- [ ] **Step 4:** Run → PASS (all 4 tests). This is the highest-risk code — if a test fails, fix the byte offsets against the spike, not the test.

---

## Task 4: OAuth token exchange / refresh + persistence

**Files:**
- Create: `src/orchestrator/platform/epic/oauth.py`
- Test: `tests/platform/epic/test_oauth.py`

`oauth.py` holds the httpx token calls + refresh-token file persistence. A `_build_transport()` seam (like F5) lets tests inject `httpx.MockTransport`. Token-file at `{epic_session_dir}/refresh_token` (0600).

- [ ] **Step 1: Write failing tests** (MockTransport for the token endpoint; tmp session dir via `tmp_path`):

```python
import json
import httpx
import pytest
from orchestrator.platform.epic import oauth as ep_oauth
from orchestrator.platform.epic.models import AuthTokens

pytestmark = pytest.mark.asyncio

def _token_response(account="acc", display="Karl"):
    return httpx.Response(200, json={
        "access_token": "ACCESS", "refresh_token": "REFRESH",
        "account_id": account, "displayName": display,
        "expires_at": "2026-06-03T01:00:00.000Z",
    })

async def test_exchange_code_returns_tokens(monkeypatch):
    def handler(req):
        assert req.url.path.endswith("/oauth/token")
        body = req.content.decode()
        assert "grant_type=authorization_code" in body and "code=THECODE" in body
        return _token_response()
    monkeypatch.setattr(ep_oauth, "_build_transport", lambda: httpx.MockTransport(handler))
    tokens = await ep_oauth.exchange_code("THECODE", _settings())
    assert isinstance(tokens, AuthTokens)
    assert tokens.access_token == "ACCESS" and tokens.refresh_token == "REFRESH"

async def test_refresh_uses_refresh_token(monkeypatch):
    def handler(req):
        assert "grant_type=refresh_token" in req.content.decode()
        return _token_response()
    monkeypatch.setattr(ep_oauth, "_build_transport", lambda: httpx.MockTransport(handler))
    tokens = await ep_oauth.refresh("OLDREFRESH", _settings())
    assert tokens.access_token == "ACCESS"

async def test_refresh_failure_raises_epicauth(monkeypatch):
    monkeypatch.setattr(ep_oauth, "_build_transport",
                        lambda: httpx.MockTransport(lambda r: httpx.Response(400, json={"errorCode": "x"})))
    with pytest.raises(ep_oauth.EpicAuthError):
        await ep_oauth.refresh("BAD", _settings())

def test_persist_and_load_refresh_token(tmp_path):
    ep_oauth.save_refresh_token(str(tmp_path), "RT-123")
    f = tmp_path / "refresh_token"
    assert f.read_text() == "RT-123"
    assert (f.stat().st_mode & 0o777) == 0o600
    assert ep_oauth.load_refresh_token(str(tmp_path)) == "RT-123"

def test_load_missing_returns_none(tmp_path):
    assert ep_oauth.load_refresh_token(str(tmp_path)) is None
```

Add a `_settings()` helper at module top: `from orchestrator.core.settings import Settings; def _settings(): return Settings(orchestrator_token="a"*32)`.

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** `oauth.py`:

```python
"""Epic OAuth (F6): authorization_code + refresh_token grants, token persistence.

Pure async httpx. The client_id/secret are the public legendary launcher creds.
Access/refresh tokens are secret — never put them in log event fields.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from orchestrator.platform.epic.models import AuthTokens

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)

_REFRESH_FILE = "refresh_token"


class EpicAuthError(Exception):
    """Epic OAuth exchange/refresh failed."""


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject httpx.MockTransport. None → real network."""
    return None


def _client(settings: Settings) -> httpx.AsyncClient:
    transport = _build_transport()
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(30.0, connect=10.0),
        "headers": {"User-Agent": settings.epic_user_agent},
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def _to_tokens(d: dict[str, Any]) -> AuthTokens:
    return AuthTokens(
        access_token=d["access_token"],
        refresh_token=d.get("refresh_token", ""),
        account_id=str(d.get("account_id", "")),
        display_name=str(d.get("displayName", d.get("account_id", "unknown"))),
        expires_at=str(d.get("expires_at", "")),
    )


async def _grant(data: dict[str, str], settings: Settings, *, what: str) -> AuthTokens:
    async with _client(settings) as client:
        resp = await client.post(
            settings.epic_token_url,
            auth=(settings.epic_client_id, settings.epic_client_secret),
            data=data,
        )
    if resp.status_code != 200:
        # Body may include the rejected token — log only status + errorCode.
        code = ""
        try:
            code = str(resp.json().get("errorCode", ""))
        except Exception:  # noqa: BLE001 - body may not be JSON
            pass
        _log.warning("epic.oauth.failed", what=what, status=resp.status_code, error_code=code)
        raise EpicAuthError(f"epic {what} failed: HTTP {resp.status_code} {code}")
    return _to_tokens(resp.json())


async def exchange_code(code: str, settings: Settings) -> AuthTokens:
    return await _grant(
        {"grant_type": "authorization_code", "code": code, "token_type": "eg1"},
        settings, what="exchange_code",
    )


async def refresh(refresh_token: str, settings: Settings) -> AuthTokens:
    return await _grant(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "token_type": "eg1"},
        settings, what="refresh",
    )


def save_refresh_token(session_dir: str, refresh_token: str) -> None:
    d = Path(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / _REFRESH_FILE
    # Write 0600 atomically (create with mode, then write).
    fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, refresh_token.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(str(f), 0o600)


def load_refresh_token(session_dir: str) -> str | None:
    f = Path(session_dir) / _REFRESH_FILE
    if not f.is_file():
        return None
    return f.read_text(encoding="utf-8").strip() or None
```

- [ ] **Step 4:** Run → PASS.

---

## Task 5: Library enumeration + manifest URL fetch

**Files:**
- Modify: `src/orchestrator/platform/epic/manifest.py` (add async `fetch_manifest`)
- Create: `src/orchestrator/platform/epic/library.py`
- Test: `tests/platform/epic/test_library.py`, `tests/platform/epic/test_manifest_fetch.py`

- [ ] **Step 1: Write failing tests** for `library.enumerate` (paginated cursor) and `manifest.fetch_manifest` (v2 API → manifest URI + cdn_base → binary download → parse). Both take an `access_token` + `Settings`, use a `_build_transport()` seam:

```python
# test_library.py
import httpx, pytest
from orchestrator.platform.epic import library as ep_lib
pytestmark = pytest.mark.asyncio

async def test_enumerate_paginates(monkeypatch):
    pages = {
        None: {"records": [{"appName": "A", "namespace": "ns", "catalogItemId": "c1"}],
               "responseMetadata": {"nextCursor": "CUR"}},
        "CUR": {"records": [{"appName": "B", "namespace": "ns", "catalogItemId": "c2"}],
                "responseMetadata": {}},
    }
    def handler(req):
        cur = dict(req.url.params).get("cursor")
        return httpx.Response(200, json=pages[cur])
    monkeypatch.setattr(ep_lib, "_build_transport", lambda: httpx.MockTransport(handler))
    items = await ep_lib.enumerate_library("TOK", _settings())
    assert [i.app_name for i in items] == ["A", "B"]
    assert items[0].namespace == "ns"
```

```python
# test_manifest_fetch.py — uses _build_manifest() from test_manifest_parse (copy the helper
# into this file or a tests/platform/epic/_manifest_fixtures.py shared module)
import httpx, pytest
from orchestrator.platform.epic import manifest as ep_man
from orchestrator.platform.epic.models import EpicLibraryItem
pytestmark = pytest.mark.asyncio

async def test_fetch_manifest_returns_parsed_and_cdn_base(monkeypatch):
    raw = _build_manifest(22, _chunks(1))
    def handler(req):
        if "/assets/v2/" in req.url.path:
            return httpx.Response(200, json={"elements": [{"manifests": [
                {"uri": "https://epiccdn.test/abc/def.manifest",
                 "queryParams": [{"name": "k", "value": "v"}]}]}]})
        # manifest binary download
        assert req.url.host == "epiccdn.test"
        return httpx.Response(200, content=raw)
    monkeypatch.setattr(ep_man, "_build_transport", lambda: httpx.MockTransport(handler))
    item = EpicLibraryItem(app_name="A", namespace="ns", catalog_item_id="c", title="A")
    m, cdn_host, cdn_base = await ep_man.fetch_manifest("TOK", item, _settings())
    assert m.version == 22 and len(m.chunks) == 1
    assert cdn_host == "epiccdn.test"
    assert cdn_base == "/abc"  # dir of the manifest path
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** `library.py` (`enumerate_library`) + add `_build_transport()`, `fetch_manifest()` and a shared `_client()` to `manifest.py`. `fetch_manifest` returns `(EpicManifest, cdn_host, cdn_base_path)`; build the v2 URL from `settings.epic_manifest_url_template`, append `queryParams`, GET the signed manifest URI, `parse_manifest(resp.content)`, derive host + `path.rsplit("/",1)[0]` from the manifest URI. Port the request shapes from the spike's `list_library`/`get_manifest_url`. Headers: `Authorization: bearer {token}` + `User-Agent`. Raise `EpicManifestError` / a new `EpicLibraryError` on non-200.

- [ ] **Step 4:** Run → PASS.

---

## Task 6: EpicClient + Deps wiring

**Files:**
- Create: `src/orchestrator/platform/epic/client.py`
- Modify: `src/orchestrator/jobs/worker.py` (add `epic_client: EpicClient | None` to `Deps`)
- Modify: `src/orchestrator/api/main.py` (construct `EpicClient`, pass to `JobsDeps` + `app.state`)
- Test: `tests/platform/epic/test_client.py`

`EpicClient` holds `Settings`, lazily refreshes the access token from the stored refresh token, and exposes `library_enumerate()`, `fetch_manifest(item)`, `auth_status()`. Handlers use `deps.epic_client`.

- [ ] **Step 1: Write failing test:** construct `EpicClient(settings)` with `monkeypatch` on `oauth.refresh`/`load_refresh_token` + `library.enumerate_library`; assert `await client.library_enumerate()` refreshes once and returns items; assert `EpicNotAuthenticated` raised when no refresh token on disk.

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** `client.py`:

```python
"""EpicClient (F6) — token lifecycle + library/manifest facade for handlers.

Mirrors SteamWorkerClient's role in Deps, but is pure async httpx (no subprocess).
Caches a valid access token; refreshes from the persisted refresh token on demand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from orchestrator.platform.epic import library, manifest, oauth
from orchestrator.platform.epic.models import AuthTokens

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings
    from orchestrator.platform.epic.models import EpicLibraryItem, EpicManifest

_log = structlog.get_logger(__name__)


class EpicNotAuthenticated(Exception):
    """No usable Epic session (no stored refresh token, or refresh rejected)."""


class EpicClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tokens: AuthTokens | None = None

    async def _access_token(self) -> str:
        if self._tokens is not None:
            return self._tokens.access_token
        rt = oauth.load_refresh_token(self._settings.epic_session_dir)
        if rt is None:
            raise EpicNotAuthenticated("no stored Epic refresh token")
        try:
            self._tokens = await oauth.refresh(rt, self._settings)
        except oauth.EpicAuthError as e:
            raise EpicNotAuthenticated(str(e)) from e
        if self._tokens.refresh_token:
            oauth.save_refresh_token(self._settings.epic_session_dir, self._tokens.refresh_token)
        return self._tokens.access_token

    async def library_enumerate(self) -> list[EpicLibraryItem]:
        return await library.enumerate_library(await self._access_token(), self._settings)

    async def fetch_manifest(self, item: EpicLibraryItem) -> tuple[EpicManifest, str, str]:
        return await manifest.fetch_manifest(await self._access_token(), item, self._settings)
```

- [ ] **Step 4:** Add `epic_client: EpicClient | None` to the `Deps` dataclass (`jobs/worker.py`); import under `TYPE_CHECKING`. In `main.py`, build `EpicClient(settings)` and pass to `JobsDeps(pool=..., steam_client=..., epic_client=...)` and stash on `app.state.epic_client`. **Every existing `Deps(...)` / `JobsDeps(...)` construction in tests must add `epic_client=None`** — grep `grep -rn "Deps(" tests/ src/` and update each (or give `epic_client` a `None` default in the dataclass to avoid churn — prefer the default since `steam_client` is already nullable).

- [ ] **Step 5:** Run `Run: .venv/bin/pytest tests/platform/epic/test_client.py tests/jobs/ -q` → PASS.

---

## Task 7: Epic chunk downloader

**Files:**
- Create: `src/orchestrator/prefill/epic_downloader.py`
- Test: `tests/prefill/test_epic_downloader.py`

Mirror `prefill/downloader.py` exactly (Semaphore, stream+discard, `[1,4,16]` retry, 4xx-no-retry, `_build_transport()` seam, `_FAILURE_CAP`), but route by `Host` header (the CDN host) and prepend `cdn_base_path` to each chunk path. Add `verify_cached`.

- [ ] **Step 1: Write failing tests** (`httpx.MockTransport`): assert the `Host` header == cdn_host + Epic UA; the request path == `{cdn_base}/{chunk_path}`; retry-then-success; 4xx not retried; concurrency ≤ cap; `EpicPrefillResult` totals; `verify_cached` counts `X-Upstream-Cache-Status: HIT`.

```python
import httpx, pytest
from orchestrator.prefill.epic_downloader import prefill_chunks, verify_cached, EpicPrefillResult
pytestmark = pytest.mark.asyncio

async def test_prefill_sets_host_and_path(monkeypatch):
    seen = []
    def handler(req):
        seen.append((req.headers.get("host"), req.url.path))
        return httpx.Response(200)
    monkeypatch.setattr("orchestrator.prefill.epic_downloader._build_transport",
                        lambda: httpx.MockTransport(handler))
    r = await prefill_chunks(["ChunksV5/00/a_b.chunk"], "epiccdn.test", "/base",
                             _settings(), lancache_base_url="http://127.0.0.1")
    assert isinstance(r, EpicPrefillResult) and r.chunks_ok == 1
    assert seen[0][0] == "epiccdn.test"
    assert seen[0][1] == "/base/ChunksV5/00/a_b.chunk"

async def test_verify_cached_counts_hits(monkeypatch):
    def handler(req):
        return httpx.Response(200, headers={"X-Upstream-Cache-Status": "HIT"})
    monkeypatch.setattr("orchestrator.prefill.epic_downloader._build_transport",
                        lambda: httpx.MockTransport(handler))
    ratio = await verify_cached(["ChunksV5/00/a_b.chunk"], "epiccdn.test", "/base",
                                _settings(), lancache_base_url="http://127.0.0.1")
    assert ratio == 1.0
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** `epic_downloader.py` by copying `downloader.py` and changing: signature `prefill_chunks(chunk_paths, cdn_host, cdn_base_path, settings, *, on_progress=None, lancache_base_url=None)`; `headers = {"User-Agent": settings.epic_user_agent, "Host": cdn_host}`; request path `f"{cdn_base_path.rstrip('/')}/{path}"`; `base_url = lancache_base_url or settings.lancache_base_url`; result dataclass `EpicPrefillResult` (same fields). Add `verify_cached(sample_paths, cdn_host, cdn_base_path, settings, *, lancache_base_url=None) -> float` that GETs each (no stream needed), counts `resp.headers.get("X-Upstream-Cache-Status","").upper()=="HIT"`, returns hits/total (0.0 if empty).

- [ ] **Step 4:** Run → PASS.

---

## Task 8: Handler Epic branches (library_sync, manifest_fetch, prefill)

**Files:**
- Modify: `src/orchestrator/jobs/handlers/library_sync.py`, `manifest_fetch.py`, `prefill.py`
- Test: `tests/jobs/test_epic_handlers.py`

Each handler dispatches on `job.platform`: keep the Steam path; add an `epic` branch that uses `deps.epic_client`. **Refactor each `*_handler` to a small dispatcher** that calls `_steam_*` (existing body) or `_epic_*`.

- [ ] **Step 1: Write failing tests** (stub `EpicClient` returning fixed library/manifest; monkeypatch `epic_downloader.prefill_chunks`/`verify_cached`; seeded pool). Cover:
  - `library_sync` epic: upserts `games(platform='epic', app_id=app_name, title)`.
  - `manifest_fetch` epic: stores a `manifests` row (raw bytes, version, chunk_count, total_bytes) + updates `games.size_bytes`.
  - `prefill` epic: sets `status='downloading'`, re-fetches a FRESH manifest, calls `prefill_chunks`, enqueues a `validate` job on success; sets `status='failed'` on chunk failure; raises on non-epic-with-no-epic-client.

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** the branches:
  - `library_sync.py`: `if platform == 'epic'` → `items = await deps.epic_client.library_enumerate()`; upsert each `("epic", item.app_name, item.title, json.dumps({"namespace": item.namespace, "catalog_item_id": item.catalog_item_id}))` via the existing `_UPSERT_SQL`.
  - `manifest_fetch.py`: epic branch → `m, _host, _base = await deps.epic_client.fetch_manifest(item)`; store via an Epic upsert: `manifests(game_id, depot_id=NULL, version=<build or str(m.version)>, raw=<binary>, chunk_count=len(m.chunks), total_bytes=sum file_size)`; update `games.size_bytes`. (Reuse the existing manifests UPSERT shape with `depot_id=None`.) Reconstruct the `EpicLibraryItem` from the `games` row metadata JSON.
  - `prefill.py`: epic branch → set `status='downloading'`; rebuild `EpicLibraryItem` from `games.metadata`; `m, cdn_host, cdn_base = await deps.epic_client.fetch_manifest(item)` (FRESH — signed URLs expire); `paths = [chunk_path(c, m.version) for c in m.chunks]` (dedup); `result = await prefill_chunks(paths, cdn_host, cdn_base, settings)`; on `chunks_failed==0` → `verify_cached(sample)` (log the HIT ratio), enqueue `INSERT INTO jobs (kind, game_id, platform, state, source) VALUES ('validate', ?, 'epic', 'queued', 'scheduler')`, set `last_prefilled_at`; else `status='failed'` + raise. Guard: epic branch requires `deps.epic_client` (raise `RuntimeError` if None).

- [ ] **Step 4:** Run `Run: .venv/bin/pytest tests/jobs/ -q` → PASS (existing Steam tests unaffected).

---

## Task 9: Epic auth + sync routers + wiring

**Files:**
- Create: `src/orchestrator/api/routers/epic_auth.py`, `src/orchestrator/api/routers/epic_sync.py`
- Modify: `src/orchestrator/api/main.py` (include both routers)
- Test: `tests/api/test_epic_auth_router.py`, `tests/api/test_epic_sync_router.py`

- [ ] **Step 1: Write failing tests** mirroring `tests/api/test_sync_router.py` (bearer 401, 202, dedup, 503) for `POST /api/v1/platforms/epic/library/sync`; and for `epic_auth`: `POST /api/v1/platforms/epic/auth {code}` → exchanges (monkeypatch `oauth.exchange_code` to return tokens), persists, sets `platforms.auth_status='ok'`, auto-enqueues an epic `library_sync`, returns 202; bad code → 400/401; `GET` returns the `platforms` epic row status.

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement** `epic_sync.py` by copying `routers/sync.py` and swapping `'steam'`→`'epic'` (the `idx_jobs_library_sync_inflight` partial-unique index already covers epic — per-platform). `epic_auth.py`: `POST` reads the code, `tokens = await exchange_code(code, settings)`, `save_refresh_token(settings.epic_session_dir, tokens.refresh_token)`, `UPDATE platforms SET auth_status='ok', auth_expires_at=?, last_error=NULL WHERE name='epic'`, then dedup-insert an epic `library_sync` job, return 202 `{account_id, display_name}` (NO tokens in the body). `EpicAuthError` → 401. Wire both routers in `main.py` (mirror how `sync` router is included). NEVER echo the code or tokens.

- [ ] **Step 4:** Run `Run: .venv/bin/pytest tests/api/ -q` → PASS.

---

## Task 10: Stage `epic_chunk_uri()` (deferred validator)

**Files:**
- Modify: `src/orchestrator/validator/cache_key.py`
- Test: `tests/validator/test_cache_key.py`

- [ ] **Step 1: Write failing test:** `epic_chunk_uri("ChunksV5/00/a_b.chunk", "/base")` returns `"/base/ChunksV5/00/a_b.chunk"` (URI shape only — NOT wired into disk-stat this round).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** a small pure `epic_chunk_uri(chunk_path: str, cdn_base_path: str) -> str` with a docstring noting the F7-Epic disk-stat validator is a deferred follow-up (needs the Epic on-disk cache-key derived from real cached chunks).
- [ ] **Step 4:** Run → PASS.

---

## Task 11: ADR-0014

**Files:**
- Create: `docs/ADR documentation/ADR-0014-epic-pure-python-manifest.md`

- [ ] **Step 1:** Using the ADR template in `docs/ADR documentation/`, record: **Decision** — implement Epic manifest parsing + prefill in pure Python (httpx + struct), not by vendoring `legendary`. **Context** — spike B proved it; Manifesto F6 wording said "vendored legendary modules". **Consequences** — avoids GPL-3 vendoring + gevent complexity; we own/maintain the binary parser (risk: must track EGS manifest-format changes); deliberate deviation from the Phase-0 Manifesto wording, recorded here. No code; commit with Task 13.

---

## Task 12: FEATURES + CHANGELOG (drafted, finalized in Task 13)

**Files:**
- Modify: `FEATURES.md` (mark F6 built), `CHANGELOG.md` (Added — F6 Epic CDN Prefill)

- [ ] **Step 1:** Add the F6 entry to `FEATURES.md` (mirror the F5 entry format) and a `### Added — F6 Epic CDN Prefill` block to `CHANGELOG.md` `[Unreleased]` (mirror the F5 entry: components, settings, test count, "validation = sample cache-HIT; F7-Epic disk-stat deferred", live-UAT stopping point). Finalize counts in Task 13.

---

## Task 13: Gate sweep + adversarial verify + commit + PR

- [ ] **Step 1: Mark process build-loop steps** in order as you finish: after all tests written + verified failing earlier, run `scripts/process-checklist.sh --complete-step build_loop:tests_written` then `:tests_verified_failing` then `:implemented`.
- [ ] **Step 2: Full gate sweep** (venv on PATH): `.venv/bin/pytest -q` (full suite green); `.venv/bin/ruff check src/orchestrator/ tests/`; `.venv/bin/ruff format --check src/orchestrator/ tests/`; `.venv/bin/mypy --strict src/`; `gitleaks detect --no-banner --source .`; semgrep via the pre-commit path. Fix anything red.
- [ ] **Step 3: Security audit** — write `docs/security-audits/f6-epic-prefill-security-audit.md` (token handling/redaction, refresh-token file 0600, no token in logs/responses, SSRF surface of the lancache base URL + Host routing, manifest-parser DoS bounds, OAuth error handling). Mark `build_loop:security_audit`.
- [ ] **Step 4: Adversarial verify Workflow** (3–4 lenses over the diff: token/secret handling; manifest-parser correctness vs the spike + malformed-input safety; handler/dispatch + downloader concurrency; test quality). The last three batches each surfaced a real defect — fix any material finding in-batch before committing.
- [ ] **Step 5: Docs** — finalize CHANGELOG/FEATURES counts; mark `build_loop:documentation_updated` + `build_loop:feature_recorded`.
- [ ] **Step 6: Commit** — bring A/B/C commit-structure options to the Orchestrator FIRST (per standing rule), then a single combined `feat(f6): Epic CDN prefill — full Epic stack` commit. Push; open PR against `main`. **Never `gh pr merge`** — the Orchestrator merges.
- [ ] **Step 7: Stop** — report that the autonomous deliverable (full Epic stack, unit-tested behind stubs) is shipped, and that the **live Epic UAT** (real Epic account: redeploy, `POST /platforms/epic/auth` with a `legendary.gl/epiclogin` code, run library sync → manifest fetch → prefill of a small title, confirm cache HIT) is the manual stopping point — analogous to F5's Steam 2FA.

---

## Self-Review notes

- **Spec coverage:** OAuth (T4), library (T5), manifest fetch+parse (T3/T5), downloader (T7), prefill+validate-enqueue (T8), header-HIT verify (T7/T8), auth+sync routers (T9), settings (T1), no-migration (confirmed), ADR (T11), staged `epic_chunk_uri` (T10), security audit + UAT stopping point (T13). All spec sections map to a task.
- **Type consistency:** `AuthTokens`/`EpicChunk`/`EpicManifest`/`EpicLibraryItem` defined in T2 and used unchanged in T3–T9; `EpicPrefillResult` in T7 used in T8; `fetch_manifest` returns `(EpicManifest, cdn_host, cdn_base)` consistently in T5/T6/T8; `prefill_chunks(chunk_paths, cdn_host, cdn_base_path, settings, ...)` signature consistent T7/T8.
- **Risk order:** the binary parser (T3) is implemented + hard-tested before anything depends on it.
