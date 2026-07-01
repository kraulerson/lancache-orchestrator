# Steam Manifest-Only Fetcher (DepotDownloader) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get a current, gid-aligned manifest into the durable archive for every cached Steam app — without re-downloading chunks — so the F7 validator can validate the whole prefilled library, re-runnable weekly and unattended.

**Architecture:** A subprocess-wrapping `DepotDownloaderManifestFetcher` on the data-plane **agent** runs the DepotDownloader binary in `-manifest-only` mode (manifest bytes only, no chunks), parses each depot's chunk SHA-1s, and writes `{app}_{app}_{depot}_{gid}.shas` sidecars into the durable `/manifest-archive` the validator already reads (`parse_shas` + the locator `.shas` glob are on `main` — **zero validator change**). The agent self-enumerates the app set each run from SteamPrefill's local records (auto-grows; nothing hardcoded). A control-plane job kind + `orchestrator-cli cache fetch-manifests` just *triggers* the agent run.

**Tech Stack:** Python 3.12, FastAPI (agent), Click (CLI), httpx (control→agent), DepotDownloader (self-contained .NET 8 binary), pytest/ruff/mypy. Spec: `docs/superpowers/specs/2026-06-30-steam-manifest-fetcher-design.md`.

## Global Constraints

- **Branch:** `feat/steam-manifest-fetcher-dd` (already created, off `main`; carries the design spec commit `71b68c8`).
- **Import isolation:** `manifest_fetcher.py` and the agent router **must not** import `orchestrator.api.main` or `orchestrator.db.pool` — `tests/agent/test_import_isolation.py` must stay green. Use stdlib + subprocess only in the fetcher.
- **Security:** never echo/log/store the Steam password, Steam Guard 2FA, `shared_secret`, or any token/login-key. Our code writes only manifest `.shas` files and job results — never credential material.
- **ruff:** no `assert` in `src/` (S101 → `if cond is None: raise`); bare dict returns need `-> dict[str, Any]`; line length ≤ 100.
- **Pre-commit runs** `mypy src/orchestrator` + `ruff format` — run `.venv/bin/mypy src/orchestrator` and `.venv/bin/ruff format src/orchestrator tests` and `.venv/bin/ruff check src/orchestrator tests` before each commit.
- **Full suite:** `.venv/bin/python -m pytest -q --ignore=tests/scripts` — only acceptable failure is `tests/test_licenses.py::test_all_licenses_in_allowlist` (local pip-licenses tooling gap).
- **Plan tracking:** mark each task `in_progress` (TaskUpdate) before its source edits.
- **Context7:** DepotDownloader CLI flags via Context7 `/steamre/depotdownloader`; fastapi/pydantic/click already researched this session.
- **Commit approval:** present A/B/C commit structure before each commit; Karl merges PRs (never `gh pr merge`).
- **`.shas` filename contract (locked, already on `main`):** `{app}_{app}_{depot}_{gid}.shas`, one **lowercase 40-hex SHA-1 per line**. `manifest_parser.parse_shas(text)` keeps only lines matching `^[0-9a-f]{40}$`; `manifest_locator` globs `{app}_{app}_*.{bin,shas}` and requires `len(stem.split("_")) == 4`.

---

## PHASE 0 — SPIKES (gate the build; each produces a written finding appended to this plan)

> Spikes are **investigation tasks**, not TDD. Run the steps on the agent host (`ssh root@192.168.1.40`, inside or alongside `orchestrator-agent`), record the finding in the **"Spike findings"** subsection of this plan, and let it adjust the Phase-1 tasks named in each spike. **Do not start Phase 1 until S1–S3 findings are recorded.** No production code is committed in Phase 0 (a throwaway scratch dir only).

### Task S0: Stage a pinned DepotDownloader binary (scratch)

**Goal:** get a runnable DepotDownloader on the agent host for S1–S3.

- [ ] Pick the latest stable DepotDownloader release from Context7 `/steamre/depotdownloader` / the GitHub releases (record the exact version + the linux-x64 self-contained asset URL + its sha256).
- [ ] On the agent host, download it to a throwaway dir (e.g. `/tmp/dd-spike/`), `chmod +x DepotDownloader`, and run `./DepotDownloader --help` (or no-args) to confirm it executes under the host's .NET (record the .NET requirement — self-contained build should need none).
- [ ] Record in **Spike findings → S0**: exact version, asset URL, sha256, and the run-command form. This version + sha256 feed **Task 8 (packaging)**.

### Task S1: `-manifest-only` output format + logon/rate-limit behavior  ⚠️ critical de-risk

**Goal:** confirm DD `-manifest-only` yields parseable chunk SHA-1s **and** that fetching N apps does not trip Steam's logon rate limit (DepotDownloader is a per-app process, so each invocation is its own Steam logon — unlike an in-process single-session client; with `-remember-password` these are cheap *token* logons, but this must be measured).

- [ ] Using S0's binary and a real owned app id (e.g. one already cached, from `successfullyDownloadedDepots.json`), run a manifest-only fetch. Per Context7 the relevant flags are `-app <id>`, `-manifest-only`, `-username <user>`, `-remember-password`, `-os windows`, `-osarch 64` (record the exact, current invocation; resolve `-depot`/`-manifest` usage if per-depot pinning is needed — see S3). Note the **output location + filename** DepotDownloader writes the human-readable manifest to.
- [ ] Inspect the emitted manifest. Confirm it lists, **per depot**, the **per-chunk SHA-1 hashes** (the chunk ids the validator's cache-key needs) — not just file hashes. Record a short excerpt (redact nothing — manifest data is public).
- [ ] Lock the parse: a function that reads DD's manifest text and yields, per depot, the set of lowercase 40-hex chunk SHA-1s, to be written as `{app}_{app}_{depot}_{gid}.shas`. Record the exact field/column the SHA-1s live in. **If** the SHA-1s are present → confirm the `.shas` path (zero validator change) for **Task 3**. **If** DD only emits file-level hashes (not chunk SHA-1s) → record this as a blocker and the `.bin` fallback option (DD's raw `.manifest` protobuf → reuse `parse_chunk_shas`), to be resolved with Karl before Task 3.
- [ ] **Rate-limit measurement:** run the manifest-only fetch back-to-back for ~20 distinct apps with **no delay**, then with a **3 s** delay; record how many succeed before any `RateLimitExceeded`/throttle, and whether `-remember-password` token-logons are throttled like the old full-credential per-app harvest was (~118). Record the **safe inter-request delay** to feed `manifest_fetch_delay_sec` in **Task 1** and `fetch_all` in **Task 3**. **If even throttled token-logons rate-limit hard (≪1077/run), STOP and escalate to Karl** — the per-app-process model may be unviable and we revisit the tool.

### Task S2: Auth — reuse SteamPrefill's session vs DD's own login

**Goal:** decide whether Karl can skip a second 2FA.

- [ ] Inspect SteamPrefill's persisted auth (`/SteamPrefill/Config/account.config`) and DepotDownloader's session/token storage (where `-remember-password` persists its login key — typically a `.DepotDownloader/` dir under the working dir or `$HOME`). Determine whether DepotDownloader can be pointed at / can consume SteamPrefill's existing SteamKit2 session token (same library family) so **no new login** is needed.
- [ ] **If reuse works:** record exactly how (which file/dir, mounted where, any conversion). This becomes the `login_from_session()` impl in **Task 2** and removes the operator 2FA step.
- [ ] **If reuse does NOT work:** record DepotDownloader's own one-time interactive login flow — `DepotDownloader -username <user> -remember-password ...` + Steam Guard prompt — and **where** it persists the login key, so we mount that dir durably (`/depotdownloader-config`, chown 1000) and every later run is unattended. Record the exact one-time `docker exec -it …` command for the operator go-live. **Security:** confirm the persisted artifact is only a token/login-key (never the plaintext password) and that our wrapper never passes the password on argv in the unattended path (only `-username … -remember-password`).
- [ ] Record the chosen `depotdownloader_config_dir` default + whether `login_from_session()` checks SteamPrefill's `account.config` (reuse) or DD's login-key file (own session) for presence → typed `SteamAuthError` when absent.

### Task S3: gid alignment + enumeration source

**Goal:** fetch manifests for the **cached** version and confirm cache-key paths match real files; pin the enumeration source.

- [ ] **Enumeration source:** confirm the app set. The locator module docstring warns `successfullyDownloadedDepots.json` "omits apps that have cached manifests and lists only a subset of an app's depot gids" — so as an enumeration index it may be **incomplete**. Compare, on the agent: the key-set of `successfullyDownloadedDepots.json` vs `selectedAppsToPrefill.json` (1077) vs the distinct apps with cached chunks. Record which source (or the **union**) is the complete "what to fetch" set for **Task 3**'s `_enumerate_app_ids()`.
- [ ] **gid↔depot + cached-gid:** for one app, get DD's `-manifest-only` per-depot output (which gives depot **and** gid directly) and compare its gid against the gid in `successfullyDownloadedDepots.json` for that app. Decide: (a) write the gid DD reports (current branch — simplest, DD supplies depot+gid) or (b) pin to the cached gid via `-manifest <gid>` (true cached-version validation, needs the gid↔depot mapping). Record the decision for **Task 3**. (Default expectation: option (a); the validator's `prefilled_gids` preference still pins validate to the cached gid when `downloaded_state()` has a record, and falls back to the fetched `.shas` otherwise.)
- [ ] **Cross-check:** for one app, write its `.shas` into a scratch archive dir, point a `steam_validate` at it (`steam_manifest_archive_dir=<scratch>`), and confirm the computed lancache cache-key paths hit **real files on disk** (cached → high cached/total; matches a SteamPrefill `.bin` validate of the same app where one exists). This proves the `.shas` SHA-1s derive the same cache keys as the `.bin` path. Record pass/fail.

### Spike findings (recorded 2026-06-30 — all run credential-free on the agent host)

**S0 — binary.** DepotDownloader **3.4.0** (latest, 2025-05-09), linux-x64 **self-contained** (no host .NET needed; host has none). Asset: `https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_3.4.0/DepotDownloader-linux-x64.zip`, **zip sha256 `a999dec66b4850fc961bd50366696d23c2d0fad7b18790e6a5647b2f19097a53`**. Unzips to a single `DepotDownloader` binary (78 MB). Runs on the host. → feeds **Task 8**.

**S1 — output/parse (KEY CHANGE).** `-manifest-only` writes **two** files per depot in `<cwd>/depots/<depot>/<id>/`: a human-readable `manifest_<depot>_<gid>.txt` (**file-level** SHAs only — 2917 files, NOT the chunks) and the raw binary `.DepotDownloader/<depot>_<gid>.manifest` (**SteamKit2 `ContentManifestPayload`**). The chunk SHA-1s the validator needs are **only in the raw `.manifest`**, stored as **20 raw bytes** in `ChunkData.sha`. The existing `parse_chunk_shas` returns **0** on it (SteamPrefill's `.bin` stores ChunkIds as hex *strings* — different wire format). A new ~25-line stdlib parser (`parse_steamkit_manifest`, **Task 2b**) extracts them: skip the 8-byte section header (magic `0x71F617D0` + u32 len), walk Payload field 1 (FileMapping) → field 6 (ChunkData) → field 1 (sha, 20 bytes) → `.hex()`. **Verified: extracts exactly 59495 unique chunk SHA-1s** for CS2 depot 2347770 (== the manifest's "Total number of chunks"), all valid 40-hex. Write as `.shas` → **zero validator change** downstream. **Rate-limit:** DepotDownloader is one process per `-app`, so N apps = N logons. **12 back-to-back anonymous logons: 12 OK, 0 rate-limited, 48 s** (~4 s/iter natural pacing from the manifest download itself). The old per-app SteamPrefill harvest died on full *credential* logons; `-remember-password` token logons are light. At-scale authenticated confirmation happens at go-live; the `manifest_fetch_delay_sec` knob + per-app isolation cover it. **No blocker — build proceeds.**

**S2 — auth (reuse NOT viable → one Karl 2FA).** DepotDownloader persists its session via `AccountSettingsStore` (SteamKit2 GuardData / `CMsgClientNewLoginKey`) under a `.DepotDownloader/` dir **in its working directory** — a different format from SteamPrefill's `/SteamPrefill/Config/account.config`. **No clean reuse.** → Use DD's own one-time interactive login (`-username <user> -remember-password` + Steam Guard), run from a fixed working dir we mount durably (`depotdownloader_config_dir`, the CWD; DD writes `.DepotDownloader/` under it). **Karl's one-time 2FA IS required** (at go-live). `login_from_session()` checks for DD's persisted session file under `config_dir` → `SteamAuthError` if absent. **Security:** the unattended path passes only `-username … -remember-password` (never the password on argv); DD persists only the token/guard-data, never the plaintext password.

**S3 — gid alignment + enumeration + cross-check (PASS).** DD enumerates each app's depots + current public-branch gids itself and names outputs `manifest_<depot>_<gid>` — so **the fetcher gets depot+gid directly; no `successfullyDownloadedDepots` gid↔depot mapping needed**. It writes the **current public gid** (option a); the validator's `prefilled_gids` preference (from `downloaded_state()`) still pins validate to the cached gid when a record exists, else uses the fetched `.shas`. **Cross-check: 400/400** sampled CS2 depot-2347770 chunk SHAs → lancache cache-keys → **present on disk** (`/data/cache/cache/33/56/…`) — DD's chunk SHAs derive the exact same cache keys as SteamPrefill's path. **Enumeration source** for `_enumerate_app_ids`: the app-id **union** of `successfullyDownloadedDepots.json` keys + `selectedAppsToPrefill.json` (both auto-grow); DD resolves depots per app. (Note: depot 731 failed on a CDN host `cache1-blv2.valve.org` DNS — a lancache-routing quirk; add `-use-lancache` and/or confirm CDN-domain routing at go-live.)

---

## PHASE 1 — BUILD (TDD, mocked DepotDownloader binary)

> Unit tests use a **fake DepotDownloader binary** (a shell script that emits a canned manifest), mirroring `tests/platform/steam/test_prefill_driver.py`'s `_fake_binary`. **No live Steam in unit tests.**

### Task 1: Settings — fetcher config fields

**Files:**
- Modify: `src/orchestrator/core/settings.py` (after line 124, the SteamPrefill/manifest block)
- Test: `tests/core/test_settings.py`

**Interfaces:**
- Produces: `Settings.depotdownloader_binary: Path`, `Settings.depotdownloader_config_dir: Path`, `Settings.manifest_fetch_delay_sec: float` (env: `ORCH_DEPOTDOWNLOADER_BINARY`, `ORCH_DEPOTDOWNLOADER_CONFIG_DIR`, `ORCH_MANIFEST_FETCH_DELAY_SEC`).

- [ ] **Step 1: Write the failing test** — append to `tests/core/test_settings.py`:

```python
def test_manifest_fetcher_settings_defaults():
    s = Settings(orchestrator_token="a" * 32)
    assert s.depotdownloader_binary == Path("/depotdownloader/DepotDownloader")
    assert s.depotdownloader_config_dir == Path("/depotdownloader-config")
    assert s.manifest_fetch_delay_sec == 3.0


def test_manifest_fetcher_settings_env_override(monkeypatch):
    monkeypatch.setenv("ORCH_MANIFEST_FETCH_DELAY_SEC", "5.5")
    monkeypatch.setenv("ORCH_DEPOTDOWNLOADER_CONFIG_DIR", "/custom/dd")
    s = Settings(orchestrator_token="a" * 32)
    assert s.manifest_fetch_delay_sec == 5.5
    assert s.depotdownloader_config_dir == Path("/custom/dd")
```

- [ ] **Step 2: Run to verify it fails** — `\.venv/bin/python -m pytest tests/core/test_settings.py::test_manifest_fetcher_settings_defaults -v` → FAIL (`AttributeError`, no such field). _(The `manifest_fetch_delay_sec` default `3.0` is a placeholder — replace with the S1-measured safe delay before Step 4.)_

- [ ] **Step 3: Implement** — add to `Settings` after the `manifest_archive_sync_interval_sec` field:

```python
    # --- DepotDownloader manifest-only fetcher (validation-coverage gap) -----
    # Fetches manifests (NO chunks) for the cached library so validate covers
    # apps SteamPrefill skipped (already-up-to-date apps never (re)write a .bin).
    # Self-contained .NET 8 binary; writes .shas sidecars into the archive.
    depotdownloader_binary: Path = Path("/depotdownloader/DepotDownloader")
    depotdownloader_config_dir: Path = Path("/depotdownloader-config")
    # Inter-request delay (seconds) between per-app DepotDownloader invocations.
    # DepotDownloader is a per-app process (each run is its own Steam logon), so
    # the run is throttled to stay under Steam's logon rate limit. Value tuned by
    # spike S1. 0 disables the delay.
    manifest_fetch_delay_sec: float = Field(default=3.0, ge=0.0)
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/core/test_settings.py -q` → PASS.
- [ ] **Step 5: mypy + ruff** — `.venv/bin/mypy src/orchestrator && .venv/bin/ruff format src/orchestrator tests && .venv/bin/ruff check src/orchestrator tests`.
- [ ] **Step 6:** (no commit yet — Task 8 does the single feature commit per Karl's preference; if executing with per-task commits, follow the commit-approval gate.)

### Task 2: Fetcher skeleton — `SteamAuthError`, `FetchResult`, `login_from_session()`

**Files:**
- Create: `src/orchestrator/platform/steam/manifest_fetcher.py`
- Test: `tests/platform/steam/test_manifest_fetcher.py`

**Interfaces:**
- Produces:
  - `class SteamAuthError(Exception)` — raised when no usable session.
  - `@dataclass(frozen=True) class FetchResult: fetched: int; skipped: int; failed: int; apps: int`
  - `class DepotDownloaderManifestFetcher` with `__init__(self, *, binary: Path, config_dir: Path, steam_config_dir: Path, archive_dir: Path, delay_sec: float = 0.0)` and `def login_from_session(self) -> None`.
- Consumes: Settings fields from Task 1 + existing `steam_prefill_config_dir`, `steam_manifest_archive_dir`.

> **S2 adjusts `login_from_session()`:** reuse-path checks SteamPrefill's `account.config`; own-session path checks DD's login-key file under `config_dir`. Written below for the **own-session** default; swap the checked path per the S2 finding.

- [ ] **Step 1: Write the failing test:**

```python
import pytest
from pathlib import Path
from orchestrator.platform.steam.manifest_fetcher import (
    DepotDownloaderManifestFetcher, SteamAuthError, FetchResult,
)

def _fetcher(tmp_path, **kw):
    return DepotDownloaderManifestFetcher(
        binary=tmp_path / "DepotDownloader",
        config_dir=kw.get("config_dir", tmp_path / "dd-config"),
        steam_config_dir=kw.get("steam_config_dir", tmp_path / "Config"),
        archive_dir=kw.get("archive_dir", tmp_path / "archive"),
        delay_sec=0.0,
    )

def test_login_from_session_raises_when_no_session(tmp_path):
    f = _fetcher(tmp_path)  # config_dir has no login key
    with pytest.raises(SteamAuthError):
        f.login_from_session()

def test_login_from_session_ok_when_session_present(tmp_path):
    cfg = tmp_path / "dd-config"
    cfg.mkdir()
    (cfg / "account.config").write_bytes(b"\x00token")  # S2: the persisted login key
    _fetcher(tmp_path, config_dir=cfg).login_from_session()  # no raise

def test_fetch_result_fields():
    r = FetchResult(fetched=3, skipped=1, failed=0, apps=4)
    assert (r.fetched, r.skipped, r.failed, r.apps) == (3, 1, 0, 4)
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/platform/steam/test_manifest_fetcher.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** the module skeleton:

```python
"""DepotDownloaderManifestFetcher — fetch Steam manifests ONLY (no chunks) via
the DepotDownloader binary, writing {app}_{app}_{depot}_{gid}.shas sidecars into
the durable manifest archive so the F7 validator covers apps SteamPrefill skips
(already-up-to-date apps never (re)write a manifest). STDLIB + subprocess only;
MUST NOT import orchestrator.api.* / orchestrator.db.* (agent import-isolation,
tests/agent/test_import_isolation.py). NEVER logs/writes the Steam password,
2FA, or any token — only manifest .shas files."""

from __future__ import annotations

import json
import subprocess  # noqa: S404  controlled argv, no shell, no user input
from dataclasses import dataclass
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


class SteamAuthError(Exception):
    """No usable DepotDownloader/SteamPrefill session — operator must log in once."""


@dataclass(frozen=True)
class FetchResult:
    fetched: int
    skipped: int
    failed: int
    apps: int


# S2: the persisted login-key filename DepotDownloader writes under config_dir
# (own-session path) — or SteamPrefill's account.config (reuse path). Confirm in S2.
_SESSION_MARKER = "account.config"


class DepotDownloaderManifestFetcher:
    def __init__(
        self,
        *,
        binary: Path,
        config_dir: Path,
        steam_config_dir: Path,
        archive_dir: Path,
        delay_sec: float = 0.0,
    ) -> None:
        self._binary = Path(binary)
        self._config_dir = Path(config_dir)
        self._steam_config_dir = Path(steam_config_dir)
        self._archive_dir = Path(archive_dir)
        self._delay_sec = delay_sec

    def login_from_session(self) -> None:
        """Verify a usable persisted session exists (no password, no 2FA).
        Raises SteamAuthError when absent so the caller surfaces 're-auth needed'
        instead of prompting in an unattended run."""
        if not (self._config_dir / _SESSION_MARKER).exists():
            raise SteamAuthError("no DepotDownloader session — run the one-time login")
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/platform/steam/test_manifest_fetcher.py -q` → PASS.
- [ ] **Step 5: import-isolation guard** — `pytest tests/agent/test_import_isolation.py -q` → PASS (the module imports no `orchestrator.api`/`orchestrator.db`).
- [ ] **Step 6: mypy + ruff** (as Task 1 Step 5).

### Task 2b: SteamKit2 manifest parser (`parse_steamkit_manifest`)

**Files:**
- Create: `src/orchestrator/platform/steam/steamkit_manifest_parser.py`
- Test: `tests/platform/steam/test_steamkit_manifest_parser.py`

**Interfaces:**
- Produces: `def parse_steamkit_manifest(data: bytes) -> set[str]` — chunk SHA-1 hex set from DepotDownloader's raw `.manifest` (SteamKit2 `ContentManifestPayload`). Stdlib only.

> **Locked by S1.** DepotDownloader's raw `.manifest` = `[u32 magic 0x71F617D0][u32 len][payload protobuf]…`; `ChunkData.sha` is 20 raw bytes. Verified to extract 59495/59495 chunk SHAs for CS2 depot 2347770.

- [ ] **Step 1: Write the failing test** — build a minimal SteamKit2 payload in-bytes (one FileMapping → two ChunkData with known 20-byte SHAs) wrapped in the magic+len header, assert the hex set:

```python
import struct
from orchestrator.platform.steam.steamkit_manifest_parser import parse_steamkit_manifest

def _tag(field, wire): return bytes([(field << 3) | wire])
def _ld(field, payload): return _tag(field, 2) + bytes([len(payload)]) + payload

def test_parse_extracts_chunk_sha1s_from_payload():
    sha_a = bytes.fromhex("aa" * 20); sha_b = bytes.fromhex("bb" * 20)
    chunk_a = _ld(1, sha_a)            # ChunkData.sha (field 1) = 20 raw bytes
    chunk_b = _ld(1, sha_b)
    filemap = _ld(6, chunk_a) + _ld(6, chunk_b)   # FileMapping.chunks (field 6), repeated
    payload = _ld(1, filemap)         # Payload.mappings (field 1)
    blob = struct.pack("<II", 0x71F617D0, len(payload)) + payload
    assert parse_steamkit_manifest(blob) == {"aa" * 20, "bb" * 20}

def test_parse_ignores_non_payload_sections_and_bad_input():
    assert parse_steamkit_manifest(b"") == set()
    assert parse_steamkit_manifest(b"\x00\x01\x02") == set()  # too short, no raise
```

- [ ] **Step 2: Run to verify fail** → module missing.
- [ ] **Step 3: Implement** `steamkit_manifest_parser.py`:

```python
"""Parse DepotDownloader's raw .manifest (SteamKit2 ContentManifestPayload) ->
chunk SHA1 hex set. Sections are [u32 magic][u32 len][protobuf]; the payload
(magic 0x71F617D0) holds repeated FileMapping (field 1), each with repeated
ChunkData (field 6), each whose sha (field 1) is 20 raw bytes. Pure stdlib; a
malformed buffer yields an empty set (never raises). (DepotDownloader's
human-readable .txt has only file-level SHAs — not the chunks — so we parse the
binary; the existing parse_chunk_shas reads SteamPrefill's different .bin format.)"""

from __future__ import annotations

import struct

_PAYLOAD_MAGIC = 0x71F617D0


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


def _ld_fields(b: bytes) -> list[tuple[int, bytes]]:
    """[(field_num, payload)] for wire-type-2 fields; skip the rest."""
    i = 0
    n = len(b)
    out: list[tuple[int, bytes]] = []
    while i < n:
        tag, i = _read_varint(b, i)
        field, wire = tag >> 3, tag & 0x7
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
        else:
            break
    return out


def parse_steamkit_manifest(data: bytes) -> set[str]:
    shas: set[str] = set()
    i = 0
    try:
        while i + 8 <= len(data):
            magic, ln = struct.unpack_from("<II", data, i)
            i += 8
            body = data[i : i + ln]
            i += ln
            if magic != _PAYLOAD_MAGIC:
                continue
            for f1, filemap in _ld_fields(body):  # Payload.mappings
                if f1 != 1:
                    continue
                for f2, chunk in _ld_fields(filemap):  # FileMapping.chunks
                    if f2 != 6:
                        continue
                    for f3, val in _ld_fields(chunk):  # ChunkData.sha
                        if f3 == 1 and len(val) == 20:
                            shas.add(val.hex())
    except (IndexError, struct.error):
        return shas
    return shas
```

- [ ] **Step 4: Run to verify pass.**  **Step 5: mypy + ruff + import-isolation.**

### Task 3: `fetch_all()` — enumerate, fetch, parse, write, isolate

**Files:**
- Modify: `src/orchestrator/platform/steam/manifest_fetcher.py`
- Test: `tests/platform/steam/test_manifest_fetcher.py`

**Interfaces:**
- Produces: `def fetch_all(self) -> FetchResult` + internal helpers `_enumerate_app_ids() -> list[int]`, `_run_manifest_only(app_id) -> list[tuple[int, str, set[str]]]` (returns `[(depot_id, gid, chunk_shas)]`), `_write_shas(app_id, depot_id, gid, shas) -> bool` (True=written, False=skipped-existing).
- Consumes: `login_from_session()`, the `.shas` filename contract, `manifest_parser`-style SHA-1 validation.

> **S1 adjusts `_run_manifest_only`** (exact DD invocation + parse). **S3 adjusts `_enumerate_app_ids`** (source: `successfullyDownloadedDepots.json` keys ∪ `selectedAppsToPrefill.json`) and whether to pin the cached gid. Written below for the expected outcome; align with the recorded findings.

- [ ] **Step 1: Write the failing tests:**

```python
import time
from orchestrator.platform.steam.manifest_fetcher import DepotDownloaderManifestFetcher

_SHA_A = "a" * 40
_SHA_B = "b" * 40

def _fetcher_with_fake_dd(tmp_path, manifests):
    """manifests: {app_id: [(depot_id, gid, [shas])]} the fake DD 'returns'."""
    cfg = tmp_path / "dd-config"; cfg.mkdir()
    (cfg / "account.config").write_bytes(b"\x00")
    steam_cfg = tmp_path / "Config"; steam_cfg.mkdir()
    (steam_cfg / "successfullyDownloadedDepots.json").write_text(
        json.dumps({str(a): [g for _d, g, _s in v] for a, v in manifests.items()})
    )
    f = DepotDownloaderManifestFetcher(
        binary=tmp_path / "DepotDownloader", config_dir=cfg,
        steam_config_dir=steam_cfg, archive_dir=tmp_path / "archive", delay_sec=0.0,
    )
    # Monkeypatch the per-app DD call to return the canned manifests (S1 locks the
    # real subprocess+parse; here we test enumerate/write/isolate/idempotency).
    f._run_manifest_only = lambda app_id: [  # type: ignore[method-assign]
        (d, g, set(s)) for (d, g, s) in manifests.get(app_id, [])
    ]
    return f

def test_fetch_all_writes_shas_per_depot(tmp_path):
    f = _fetcher_with_fake_dd(tmp_path, {440: [(441, "777", [_SHA_A, _SHA_B])]})
    r = f.fetch_all()
    out = tmp_path / "archive" / "v1" / "440_440_441_777.shas"
    assert out.exists()
    assert sorted(out.read_text().split()) == sorted([_SHA_A, _SHA_B])
    assert (r.fetched, r.apps) == (1, 1)

def test_fetch_all_idempotent_skip_existing(tmp_path):
    f = _fetcher_with_fake_dd(tmp_path, {440: [(441, "777", [_SHA_A])]})
    f.fetch_all()
    r2 = f.fetch_all()  # second run: already archived
    assert r2.skipped == 1 and r2.fetched == 0

def test_fetch_all_isolates_per_app_failure(tmp_path):
    f = _fetcher_with_fake_dd(tmp_path, {440: [(441, "777", [_SHA_A])], 730: []})
    def boom(app_id):
        if app_id == 730:
            raise RuntimeError("DD blew up on 730")
        return [(441, "777", {_SHA_A})]
    f._run_manifest_only = boom  # type: ignore[method-assign]
    r = f.fetch_all()
    assert r.failed == 1 and r.fetched == 1 and r.apps == 2  # 730 failed, 440 ok

def test_fetch_all_raises_auth_when_no_session(tmp_path):
    f = _fetcher_with_fake_dd(tmp_path, {440: []})
    (f._config_dir / "account.config").unlink()
    with pytest.raises(SteamAuthError):
        f.fetch_all()
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/platform/steam/test_manifest_fetcher.py -k fetch_all -v` → FAIL (`fetch_all` undefined).

- [ ] **Step 3: Implement** — add to `DepotDownloaderManifestFetcher` (and the SHA-1 guard import):

```python
import re
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")  # COR-2: drop non-canonical chunk ids


    def _enumerate_app_ids(self) -> list[int]:
        """The cached app set, read LIVE each run (auto-grows; nothing hardcoded).
        Union of successfullyDownloadedDepots.json keys (what's cached) and
        selectedAppsToPrefill.json (selected) — S3 confirms completeness."""
        apps: set[int] = set()
        for name in ("successfullyDownloadedDepots.json", "selectedAppsToPrefill.json"):
            p = self._steam_config_dir / name
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            keys = data.keys() if isinstance(data, dict) else data
            for k in keys:
                try:
                    apps.add(int(k))
                except (TypeError, ValueError):
                    continue
        return sorted(apps)

    def _write_shas(self, app_id: int, depot_id: int, gid: str, shas: set[str]) -> bool:
        """Write {app}_{app}_{depot}_{gid}.shas (one lowercase 40-hex SHA1/line).
        Idempotent: returns False if the file already exists. Append-only archive."""
        v1 = self._archive_dir / "v1"
        v1.mkdir(parents=True, exist_ok=True)
        out = v1 / f"{app_id}_{app_id}_{depot_id}_{gid}.shas"
        if out.exists():
            return False
        clean = sorted(s for s in shas if _SHA1_RE.match(s))
        out.write_text("\n".join(clean) + ("\n" if clean else ""))
        return True

    def fetch_all(self) -> FetchResult:
        """One run: verify session, enumerate the cached app set, fetch each app's
        manifests (no chunks) and archive .shas sidecars. Per-app failures are
        isolated and counted; a hard BaseException boundary guarantees a
        timeout-style escape can never kill the agent (the ③ lesson)."""
        self.login_from_session()
        app_ids = self._enumerate_app_ids()
        fetched = skipped = failed = 0
        try:
            for i, app_id in enumerate(app_ids):
                try:
                    for depot_id, gid, shas in self._run_manifest_only(app_id):
                        if self._write_shas(app_id, depot_id, gid, shas):
                            fetched += 1
                        else:
                            skipped += 1
                except Exception as e:  # isolate one bad app, keep going
                    failed += 1
                    _log.warning(
                        "manifest_fetch.app_failed",
                        app_id=app_id, reason=f"{type(e).__name__}: {e}"[:200],
                    )
                if self._delay_sec and i + 1 < len(app_ids):
                    time.sleep(self._delay_sec)  # throttle Steam logons (S1)
        except BaseException as e:  # noqa: BLE001  ③: a gevent.Timeout-style escape must not kill the agent
            _log.error("manifest_fetch.run_aborted", reason=f"{type(e).__name__}: {e}"[:200])
            raise
        _log.info(
            "manifest_fetch.done",
            apps=len(app_ids), fetched=fetched, skipped=skipped, failed=failed,
        )
        return FetchResult(fetched=fetched, skipped=skipped, failed=failed, apps=len(app_ids))
```

> `_run_manifest_only(self, app_id: int) -> list[tuple[int, str, set[str]]]` is the **only** method that shells out to DepotDownloader; the unit tests monkeypatch it so they stay offline. **S1-locked body:** run `subprocess.run([binary, "-app", str(app_id), "-manifest-only", "-os", "windows", "-osarch", "64", "-username", <user>, "-remember-password", "-dir", <scratch>], cwd=config_dir, capture_output=True, text=True, timeout=...)` (no shell; argv only; `-username` is a config value, never the password — DD reads the remembered login key from `config_dir/.DepotDownloader`). Then for each raw manifest DD wrote at `<scratch>/depots/<depot>/<id>/.DepotDownloader/<depot>_<gid>.manifest`, parse depot+gid from the filename and `parse_steamkit_manifest(path.read_bytes())` (Task 2b) → `(depot_id, gid, chunk_shas)`. Use `-use-lancache` if S3's CDN-host note requires it. A real minimal implementation lands in this task; it is monkeypatched in the unit tests above.

- [ ] **Step 4: Run to verify pass** — `pytest tests/platform/steam/test_manifest_fetcher.py -q` → PASS (all fetch_all tests).
- [ ] **Step 5: import-isolation + mypy + ruff** — guard green; `time`/`re`/`subprocess` imports are stdlib. Note `time.sleep` in a sync method is fine (the agent runs `fetch_all` in a worker thread — see Task 4).

### Task 4: Agent endpoint `POST/GET /v1/steam/fetch-manifests` + wire fetcher into the app

**Files:**
- Modify: `src/orchestrator/agent/routers/steam.py` (add the two routes)
- Modify: `src/orchestrator/agent/app.py` (construct `app.state.manifest_fetcher` at both driver sites, ~lines 62 & 97)
- Test: `tests/agent/test_steam.py`

**Interfaces:**
- Consumes: `DepotDownloaderManifestFetcher.fetch_all()`, `AgentJobStore` (`request.app.state.agent_jobs`), `request.app.state.agent_bg_tasks`, `request.app.state.manifest_fetcher`.
- Produces: `POST /v1/steam/fetch-manifests` → `{"job_id": str}` (202); `GET /v1/steam/fetch-manifests/{job_id}` → job snapshot. Bearer-gated by the existing app-level `BearerAuthMiddleware` (no per-route dep needed).

- [ ] **Step 1: Write the failing tests** — append to `tests/agent/test_steam.py`:

```python
class _FakeFetcher:
    def __init__(self, result=None, boom=False):
        from orchestrator.platform.steam.manifest_fetcher import FetchResult
        self._result = result or FetchResult(fetched=2, skipped=1, failed=0, apps=3)
        self._boom = boom
    def fetch_all(self):
        if self._boom:
            raise RuntimeError("fetch blew up")
        return self._result

def test_fetch_manifests_requires_bearer():
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.manifest_fetcher = _FakeFetcher()
    client = TestClient(app)  # no Authorization header
    assert client.post("/v1/steam/fetch-manifests").status_code == 401

def test_fetch_manifests_runs_to_done():
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.manifest_fetcher = _FakeFetcher()
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    job_id = client.post("/v1/steam/fetch-manifests").json()["job_id"]
    for _ in range(50):
        snap = client.get(f"/v1/steam/fetch-manifests/{job_id}").json()
        if snap["state"] == "done":
            break
        time.sleep(0.02)
    assert snap["state"] == "done"
    assert snap["result"] == {"fetched": 2, "skipped": 1, "failed": 0, "apps": 3}

def test_fetch_manifests_records_failure():
    app = create_agent_app(settings=Settings(orchestrator_token="a" * 32))
    app.state.manifest_fetcher = _FakeFetcher(boom=True)
    client = TestClient(app, headers={"Authorization": "Bearer " + "a" * 32})
    job_id = client.post("/v1/steam/fetch-manifests").json()["job_id"]
    for _ in range(50):
        snap = client.get(f"/v1/steam/fetch-manifests/{job_id}").json()
        if snap["state"] == "failed":
            break
        time.sleep(0.02)
    assert snap["state"] == "failed"
```

- [ ] **Step 2: Run to verify fail** — `pytest tests/agent/test_steam.py -k fetch_manifests -v` → FAIL (404/no route).

- [ ] **Step 3: Implement** — add to `src/orchestrator/agent/routers/steam.py` (uses the pull.py bg-task pattern; `fetch_all` is sync → run in a worker thread so the event loop is never blocked):

```python
@router.post("/v1/steam/fetch-manifests", status_code=status.HTTP_202_ACCEPTED)
async def start_fetch_manifests(request: Request) -> dict[str, str]:
    fetcher = request.app.state.manifest_fetcher
    store = request.app.state.agent_jobs
    job_id = store.create()

    async def _run() -> None:
        try:
            result = await asyncio.to_thread(fetcher.fetch_all)
            store.set_done(
                job_id,
                {
                    "fetched": result.fetched,
                    "skipped": result.skipped,
                    "failed": result.failed,
                    "apps": result.apps,
                },
            )
        except Exception as e:  # record, never crash the loop
            store.set_failed(job_id, f"{type(e).__name__}: {e}"[:200])

    bg_tasks = request.app.state.agent_bg_tasks
    task = asyncio.create_task(_run())
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return {"job_id": job_id}


@router.get("/v1/steam/fetch-manifests/{job_id}")
async def get_fetch_manifests(job_id: str, request: Request) -> dict[str, Any]:
    snap: dict[str, Any] | None = request.app.state.agent_jobs.get(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="job not found")
    return snap
```

  And in `src/orchestrator/agent/app.py`, construct the fetcher next to each `SteamPrefillDriver(...)` site (both the lifespan-guarded and the eager site):

```python
from orchestrator.platform.steam.manifest_fetcher import DepotDownloaderManifestFetcher
...
    app.state.manifest_fetcher = DepotDownloaderManifestFetcher(
        binary=settings.depotdownloader_binary,
        config_dir=settings.depotdownloader_config_dir,
        steam_config_dir=settings.steam_prefill_config_dir,
        archive_dir=settings.steam_manifest_archive_dir,
        delay_sec=settings.manifest_fetch_delay_sec,
    )
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/agent/test_steam.py -q` → PASS (existing + 3 new).
- [ ] **Step 5: mypy + ruff + import-isolation.**

### Task 5: `AgentClient.fetch_manifests()`

**Files:**
- Modify: `src/orchestrator/clients/agent_client.py`
- Test: `tests/clients/test_agent_client.py`

**Interfaces:**
- Produces: `async def fetch_manifests(self) -> dict[str, Any]` — POST `/v1/steam/fetch-manifests` (no body; agent self-enumerates) then poll → returns the result dict.
- Consumes: existing `self._post_then_poll(path, payload)`.

- [ ] **Step 1: Write the failing test** (mirror the existing `steam_prefill` client test with a stub transport that returns `{"job_id": "j1"}` on POST and `{"state":"done","result":{"fetched":5,...}}` on GET):

```python
async def test_fetch_manifests_posts_and_polls():
    # reuse the existing MockTransport/handler pattern in this file:
    client = AgentClient(base_url="http://agent", token="t", transport=_stub_transport({
        ("POST", "/v1/steam/fetch-manifests"): {"job_id": "j1"},
        ("GET", "/v1/steam/fetch-manifests/j1"): {"state": "done", "result": {"fetched": 5, "skipped": 0, "failed": 0, "apps": 5}},
    }), poll_interval_sec=0.0)
    result = await client.fetch_manifests()
    assert result == {"fetched": 5, "skipped": 0, "failed": 0, "apps": 5}
```

  _(Match the actual stub/transport helper already in `tests/clients/test_agent_client.py`; the assertion is what matters.)_

- [ ] **Step 2: Run to verify fail** → `AttributeError: fetch_manifests`.
- [ ] **Step 3: Implement** — add next to `steam_prefill`:

```python
    async def fetch_manifests(self) -> dict[str, Any]:
        """Trigger a manifest-only fetch run on the agent (it self-enumerates the
        cached app set; no app-id list crosses the wire). POST + poll to done."""
        return await self._post_then_poll("/v1/steam/fetch-manifests", {})
```

  _(`_post_then_poll` POSTs `{}`; `start_fetch_manifests` ignores the body — it takes no `body` param, so an empty JSON object is accepted by FastAPI.)_

- [ ] **Step 4: Run to verify pass.**  **Step 5: mypy + ruff.**

### Task 6: Control-plane job kind + handler + registration

**Files:**
- Create: `src/orchestrator/jobs/handlers/fetch_manifests.py`
- Modify: `src/orchestrator/jobs/handlers/__init__.py` (register the kind)
- Modify: `src/orchestrator/api/routers/jobs.py:84` (add `"fetch_manifests"` to the `kind` Literal)
- Test: `tests/jobs/handlers/test_fetch_manifests.py`

**Interfaces:**
- Produces: `async def fetch_manifests_handler(job: dict[str, Any], deps: Deps) -> None`. Registered as kind `"fetch_manifests"`.
- Consumes: `Deps.agent_client.fetch_manifests()`.

- [ ] **Step 1: Write the failing test:**

```python
import pytest
from orchestrator.jobs.handlers.fetch_manifests import fetch_manifests_handler

class _StubAgent:
    def __init__(self): self.called = False
    async def fetch_manifests(self):
        self.called = True
        return {"fetched": 7, "skipped": 1, "failed": 0, "apps": 8}

class _Deps:
    def __init__(self, agent): self.agent_client = agent; self.pool = None

async def test_handler_calls_agent():
    agent = _StubAgent()
    await fetch_manifests_handler({"id": 1, "kind": "fetch_manifests"}, _Deps(agent))
    assert agent.called

async def test_handler_raises_when_agent_absent():
    with pytest.raises(ValueError):
        await fetch_manifests_handler({"id": 1}, _Deps(None))
```

- [ ] **Step 2: Run to verify fail** → module missing.
- [ ] **Step 3: Implement** `src/orchestrator/jobs/handlers/fetch_manifests.py`:

```python
"""fetch_manifests job handler — trigger the agent's DepotDownloader manifest-only
fetch (closes the validation-coverage gap). The agent self-enumerates the cached
app set; this handler just dispatches and logs the tally."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from orchestrator.jobs.worker import Deps

_log = structlog.get_logger(__name__)


async def fetch_manifests_handler(job: dict[str, Any], deps: Deps) -> None:
    """Dispatch a manifest-only fetch to the data-plane agent.

    Raises:
        ValueError — no agent client configured (agent_enabled off).
    """
    if deps.agent_client is None:
        raise ValueError("fetch_manifests requires the data-plane agent (agent_enabled)")
    result = await deps.agent_client.fetch_manifests()
    _log.info("fetch_manifests.done", job_id=job.get("id"), **result)
```

  And register in `__init__.py`'s `_register_builtin_handlers()`:

```python
    from orchestrator.jobs.handlers.fetch_manifests import fetch_manifests_handler
    ...
    register("fetch_manifests", fetch_manifests_handler)
```

  And extend the jobs-router `kind` Literal (`src/orchestrator/api/routers/jobs.py:84`):

```python
    kind: Literal["prefill", "validate", "library_sync", "auth_refresh", "sweep", "manifest_fetch", "fetch_manifests"]
```

- [ ] **Step 4: Run to verify pass** — `pytest tests/jobs/handlers/test_fetch_manifests.py -q`.
- [ ] **Step 5: mypy + ruff.**

### Task 7: Scheduler enqueue + control trigger endpoint + CLI command

**Files:**
- Modify: `src/orchestrator/scheduler/jobs.py` (add `enqueue_fetch_manifests`)
- Create: `src/orchestrator/api/routers/fetch_manifests_trigger.py` (POST `/api/v1/fetch-manifests`)
- Modify: `src/orchestrator/api/main.py` (include the new router — mirror `sweep_trigger`'s `app.include_router(...)`)
- Modify: `src/orchestrator/cli/commands/cache.py` (add `fetch-manifests` command)
- Test: `tests/scheduler/test_jobs.py`, `tests/api/test_fetch_manifests_trigger.py`, `tests/cli/test_cache_commands.py`

**Interfaces:**
- Produces: `async def enqueue_fetch_manifests(pool: Pool, *, source: str = "scheduler") -> int`; `POST /api/v1/fetch-manifests` → `202 {"job_id": int, "queued": bool}`; `cache fetch-manifests` CLI.
- Consumes: the `fetch_manifests` job kind (Task 6).

> **Dedup:** mirror `enqueue_validation_sweep` with `ON CONFLICT DO NOTHING`. A new partial-unique in-flight index for `fetch_manifests` requires a **migration** (next number after 0006). Mirror migration 0005's `idx_jobs_sweep_inflight` for `kind='fetch_manifests'`. Add the migration file + its CHECKSUM per the existing migration convention (see other `db/migrations/`); include it in this task. If the reviewer prefers no new index, the enqueue can dedup via a `WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE kind='fetch_manifests' AND state IN ('queued','running'))` guard instead — pick one and keep it consistent with the test.

- [ ] **Step 1: Write the failing tests:**

```python
# tests/scheduler/test_jobs.py
async def test_enqueue_fetch_manifests_inserts(populated_pool):
    n = await enqueue_fetch_manifests(populated_pool, source="api")
    assert n == 1
    row = await populated_pool.read_one(
        "SELECT kind, state FROM jobs WHERE kind='fetch_manifests' ORDER BY id DESC LIMIT 1")
    assert row["kind"] == "fetch_manifests" and row["state"] == "queued"

async def test_enqueue_fetch_manifests_dedups(populated_pool):
    await enqueue_fetch_manifests(populated_pool)
    n2 = await enqueue_fetch_manifests(populated_pool)
    assert n2 == 0  # in-flight dedup
```

```python
# tests/api/test_fetch_manifests_trigger.py
async def test_trigger_requires_bearer(client_no_auth):
    assert (await client_no_auth.post("/api/v1/fetch-manifests")).status_code == 401

async def test_trigger_enqueues(client):
    r = await client.post("/api/v1/fetch-manifests",
                          headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 202 and "job_id" in r.json()
```

```python
# tests/cli/test_cache_commands.py  (mirror the existing validate-all CLI test)
def test_cache_fetch_manifests_posts(monkeypatch, runner):
    posted = {}
    monkeypatch.setattr("orchestrator.cli.client.OrchClient.post",
                        lambda self, path, **kw: posted.update(path=path) or {"job_id": 7, "queued": True})
    result = runner.invoke(cache, ["fetch-manifests"], obj=_cli_ctx())
    assert result.exit_code == 0 and posted["path"] == "/api/v1/fetch-manifests"
```

  _(Match each test file's existing fixtures — `populated_pool`, `client`/`client_no_auth`, `runner`/`_cli_ctx` — to whatever those modules already use.)_

- [ ] **Step 2: Run all three to verify fail.**
- [ ] **Step 3: Implement:**

  `enqueue_fetch_manifests` in `scheduler/jobs.py` (mirror `enqueue_validation_sweep`):

```python
async def enqueue_fetch_manifests(pool: Pool, *, source: str = "scheduler") -> int:
    """Insert a `fetch_manifests` job if none is queued/running (manifest-only
    coverage fill). Mirrors enqueue_validation_sweep: at most one in-flight,
    DB-enforced via the fetch_manifests in-flight index + ON CONFLICT DO NOTHING.
    Returns rowcount (1 queued / 0 deduped). Never raises."""
    try:
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, state, source) "
            "VALUES ('fetch_manifests', 'queued', ?) ON CONFLICT DO NOTHING",
            (source,),
        )
        _log.info("scheduler.fetch_manifests.queued" if inserted else "scheduler.fetch_manifests.dedup_skip")
        return inserted
    except PoolError as e:
        _log.error("scheduler.fetch_manifests.db_error", reason=str(e)[:200])
        return 0
    except Exception as e:
        _log.error("scheduler.fetch_manifests.unexpected_error", error=type(e).__name__, reason=str(e)[:200])
        return 0
```

  `fetch_manifests_trigger.py` (mirror `sweep_trigger.py`, no `full` flag):

```python
"""POST /api/v1/fetch-manifests — enqueue a DepotDownloader manifest-only fetch
(closes the validation-coverage gap). The agent self-enumerates the cached app
set. Reuses the fetch_manifests in-flight dedup."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError
from orchestrator.scheduler.jobs import enqueue_fetch_manifests

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["fetch_manifests"])


@router.post("/fetch-manifests", responses={202: {"description": "Fetch queued"},
                                            401: {"description": "Missing/invalid bearer"},
                                            503: {"description": "Database unavailable"}})
async def trigger_fetch_manifests(pool: Pool = Depends(get_pool_dep)) -> JSONResponse:  # noqa: B008
    try:
        inserted = await enqueue_fetch_manifests(pool, source="api")
        row = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='fetch_manifests' "
            "AND state IN ('queued','running') ORDER BY id LIMIT 1")
        if row is None:
            return JSONResponse(status_code=503, content={"detail": "database unavailable"})
        _log.info("fetch_manifests_trigger.queued", job_id=int(row["id"]), queued=bool(inserted))
        return JSONResponse(status_code=202,
                            content={"job_id": int(row["id"]), "queued": bool(inserted)})
    except PoolError as e:
        _log.error("fetch_manifests_trigger.db_unavailable", reason=str(e))
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
```

  Include it in `api/main.py` next to the other `app.include_router(...)` calls (find `sweep_trigger` and add `fetch_manifests_trigger` the same way).

  CLI command in `cli/commands/cache.py` (mirror `cache_validate_all`):

```python
@cache.command("fetch-manifests")
@click.pass_context
@handles_api_errors
def cache_fetch_manifests(ctx: click.Context) -> None:
    """Fetch manifests (no chunks) for the cached library so validate covers
    apps SteamPrefill skipped. Triggers the agent's DepotDownloader run."""
    client = make_client(ctx)
    resp = client.post("/api/v1/fetch-manifests")
    job_id = resp["job_id"]
    if resp.get("queued"):
        output.success(f"queued manifest fetch (job_id={job_id}).")
    else:
        output.warn(f"a manifest fetch is already in flight (job_id={job_id}).")
```

  Add the migration `db/migrations/0007_jobs_fetch_manifests_inflight.sql` (mirror 0005's partial-unique sweep index, for `kind='fetch_manifests'`) + its CHECKSUM entry per the existing convention.

- [ ] **Step 4: Run all to verify pass** + the migrations test (`pytest tests/db -k migration -q`) green.
- [ ] **Step 5: mypy + ruff + full suite** `.venv/bin/python -m pytest -q --ignore=tests/scripts`.

### Task 8: Packaging — pin DepotDownloader into the agent image + single feature commit

**Files:**
- Modify: `Dockerfile` (runtime stage — download + verify + place the DepotDownloader binary)
- Modify: `CHANGELOG.md` (Added entry), `docs/superpowers/plans/2026-06-30-steam-manifest-fetcher-dd.md` (record spike findings)
- Create: `docs/security-audits/steam-manifest-fetcher-security-audit.md`

- [ ] **Step 1:** Add to the Dockerfile **runtime** stage (S0's exact version + sha256), before `USER orchestrator`:

```dockerfile
# DepotDownloader (self-contained .NET 8) — manifest-only fetcher for the
# validation-coverage gap. Pinned by version + sha256 (no Python dep; pip-audit
# and license checks unaffected).
ARG DEPOTDOWNLOADER_VERSION=3.4.0
ARG DEPOTDOWNLOADER_SHA256=a999dec66b4850fc961bd50366696d23c2d0fad7b18790e6a5647b2f19097a53
RUN set -eux; \
    curl -fsSL -o /tmp/dd.zip "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_${DEPOTDOWNLOADER_VERSION}/DepotDownloader-linux-x64.zip"; \
    echo "${DEPOTDOWNLOADER_SHA256}  /tmp/dd.zip" | sha256sum -c -; \
    mkdir -p /depotdownloader; \
    unzip /tmp/dd.zip -d /depotdownloader; \
    chmod +x /depotdownloader/DepotDownloader; \
    rm /tmp/dd.zip
# linux-x64 build is self-contained (bundles .NET) — no runtime .NET needed.
```

  _(If the agent's runtime base lacks `curl`/`unzip`, install them in the builder stage and COPY the binary across, or add them to the runtime `apt-get` line — keep the image lean. Confirm the .NET runtime requirement from S0; the self-contained build should bundle it.)_

- [ ] **Step 2: Verify the image builds** with the binary present (this replaces a unit test for packaging):

```bash
docker build -t orchestrator:dpa-test . && \
docker run --rm --entrypoint sh orchestrator:dpa-test -c "/depotdownloader/DepotDownloader --help >/dev/null 2>&1 && echo DD_OK"
```

Expected: `DD_OK`.

- [ ] **Step 3: Record spike findings** in this plan's "Spike findings" section; write the security audit (threat-model: argv injection — app_ids are ints from local JSON; no shell; secrets — only token/login-key persisted by DD, never by us, never logged; path traversal — archive writes are `{int}_{int}_{int}_{str-gid}.shas` under a fixed dir; availability — per-app isolation + BaseException boundary + capture never fails the job).
- [ ] **Step 4: Update `CHANGELOG.md`** (Added — Steam manifest-only fetcher closes the validation-coverage gap).
- [ ] **Step 5: Full verification** — `.venv/bin/mypy src/orchestrator` clean; `.venv/bin/ruff format && ruff check` clean; `.venv/bin/python -m pytest -q --ignore=tests/scripts` (only `test_licenses.py` fails).
- [ ] **Step 6: Single feature commit** (present A/B/C commit structure to Karl first; run the framework's mark-evaluated/process-checklist gates; Karl merges the PR).

---

## OPERATOR GO-LIVE (post-merge; Claude runs the boxes, only the 2FA — if S2 reuse failed — is Karl's)

1. **Deploy the agent image:** on `192.168.1.40`, `git pull` on `/home/karl/lancache-orchestrator`, `docker build -t orchestrator:dpa .`, add `-v depotdownloader-config:/depotdownloader-config` (named volume, chown 1000) to `/home/karl/deploy-agent.sh`, recreate the agent. (Control plane LXC 1105 also rebuilt for the new job kind/CLI; it has no DepotDownloader role.)
2. **Auth:** if **S2 reuse works**, none needed. Else **Karl** runs the one-time interactive login once: `docker exec -it orchestrator-agent <S2 DepotDownloader -username … -remember-password login command>` and completes Steam Guard. The persisted login key lands in the mounted `/depotdownloader-config` (survives recreation); every later run is unattended.
3. **Fetch:** `orchestrator-cli cache fetch-manifests` → one run, the agent self-enumerates the cached set and fetches manifests only (minutes-to-~1–1.5 h depending on the S1 delay). Confirm the archive `.shas` count grew toward the full library.
4. **Backfill validate:** `orchestrator-cli cache validate-all`. Report the **before/after `games.status` histogram** and reconcile against the ~1077+ cached expectation (was: up_to_date 1226 / not_downloaded 1363 / failed 306 / validation_failed 256).
5. **Schedule:** add a weekly `fetch_manifests` cron (the scheduler `enqueue_fetch_manifests` + the existing cron wiring) so manifests stay current and the covered set auto-grows as the library does.

---

## Self-Review

- **Spec coverage:** Component A (fetcher) → Tasks 2–3; Component B (`.shas` output, zero validator change) → Task 3 `_write_shas` + S1; Component C (trigger + enumeration) → Tasks 4–7; Component D (packaging) → Task 8; Auth model → Task 2 + S2; Spikes S1–S3 → Phase 0; Operator go-live → final section. Auto-grow/self-enumerate → Task 3 `_enumerate_app_ids`. ✓
- **Placeholder scan:** the only intentional `<...>` are S0-derived (DepotDownloader version/sha256/url) and the `_run_manifest_only` body locked by S1 — both are spike outputs by design, flagged in-place, not hand-wavy "TODO"s. ✓
- **Type consistency:** `FetchResult(fetched, skipped, failed, apps)` is identical across Tasks 2/3/4/5/6; `DepotDownloaderManifestFetcher.__init__` signature matches between Task 2 and the Task 4 app-wiring; `fetch_manifests` job kind string identical across Tasks 6/7; `.shas` filename `{app}_{app}_{depot}_{gid}.shas` identical to the locator glob on `main`. ✓
- **Risk flag (carried from S1):** DepotDownloader's per-app-process model means per-app Steam logons; S1 must prove throttled token-logons don't rate-limit at library scale, else escalate before Task 3.
