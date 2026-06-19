# Sub-project ① — Steam via SteamPrefill — Implementation Plan (RE-SCOPED)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`. **Task 1 (the live gate) is DONE — recorded below; do not re-run.**

**Goal:** Delegate Steam **prefill + owned-app enumerate** to the already-installed, modern, persistent-auth **SteamPrefill** (via a new `SteamPrefillDriver`), fixing the bulk auth-cascade. **The steam worker + Steam auth STAY** (they feed F7 validate's manifests) — full worker deletion + a modern validate-manifest source are a follow-up sub-project.

**Architecture:** A `SteamPrefillDriver` drives the SteamPrefill binary for prefill + reads its state/auth files. The prefill job's Steam path calls the driver instead of our `downloader.py`/worker-`manifest_expand`. Library sync + F8 version-diff + `/health` Steam status read SteamPrefill. Validate is untouched (still worker-fed).

**Tech Stack:** Python 3.12, SteamPrefill v3.4.2 (`/SteamPrefill/SteamPrefill`), pytest + ruff.

**Spec:** `docs/superpowers/specs/2026-06-19-steam-via-prefill-design.md` (see the RE-SCOPE banner). **Branch:** `feat/steam-via-prefill`.

---

## Context

- **No per-task commits**; single `feat` commit in the final task. `python -m pytest <path> -q` (`.venv/bin/python` if needed); `ruff check`.
- **What changes:** prefill (Steam) → driver; library_sync (Steam) → driver; F8 version-diff (Steam) → driver state; `/health` Steam status → `account.config`. **What stays:** the steam worker, `manifest_fetch`, `validate`, the Steam auth endpoints (the worker still serves validate's manifests).
- **Today's prefill path** (`jobs/handlers/prefill.py`): Steam path builds a chunk list via `deps.steam_client.manifest_expand(row["raw"])` (DB manifests) then `prefill/downloader.py:prefill_chunks` (stream-and-discard). **After:** Steam path → `driver.prefill_apps([app_id])` (SteamPrefill fetches manifest + downloads through lancache itself). The Epic path is unchanged.
- **SteamPrefill facts (recon):** binary `/SteamPrefill/SteamPrefill` v3.4.2; `/SteamPrefill/Config/{account.config (ProtoBuf: username + JWT refresh token), selectedAppsToPrefill.json (JSON list of app-id ints), successfullyDownloadedDepots.json ({app_id_str:[gid_ints]})}`; prefill flags incl. `-f|--force`, `--no-ansi`; **no `--app`** (target via `selectedAppsToPrefill.json`). Config `Config/` is shared with Karl's root cron → coordinate (lock + snapshot/restore the selection).
- **Deploy constraint (Leg C, resolve at deploy):** the orchestrator is containerized; SteamPrefill is a host binary not currently mounted. The container must **mount `/SteamPrefill`** (run the self-contained binary) + the config, OR the driver execs it on the host. Unit tests mock the binary, so code does not block on this; the final task documents the required mounts.

### Task 1 — GATE (DONE 2026-06-19, live; recorded — DO NOT re-run)
- ✅ **Leg B:** `from steam.core.manifest import DepotManifest` imports standalone in the steam venv, `gevent loaded: False` — the parser is keepable without the worker.
- ❌ **Leg A:** SteamPrefill's `~/.cache/SteamPrefill/v1/*.bin` manifests are **SteamKit2/protobuf-net format** (FileMapping field-1 is a 40-hex filename-*hash* sub-message, not a UTF-8 name) — `DepotManifest` and raw `ContentManifestPayload` both fail. **Validate cannot read SteamPrefill's cache.**
- **Outcome (Karl):** ship prefill+auth+enumerate via SteamPrefill; KEEP the worker for validate's `manifest_fetch`; defer the modern validate-manifest source + worker deletion to a follow-up. (No Task to delete the worker or remove Steam auth in this plan.)

---

### Task 2 — Settings (SteamPrefill paths)

**Files:** `core/settings.py`; `tests/core/test_settings.py`.

- [ ] **Step 1: failing test**
```python
class TestSteamPrefillSettings:
    def test_defaults(self):
        from pathlib import Path
        s = Settings(orchestrator_token="t" * 32)
        assert s.steam_prefill_binary == Path("/SteamPrefill/SteamPrefill")
        assert s.steam_prefill_config_dir == Path("/SteamPrefill/Config")
```
- [ ] **Step 2: run** `python -m pytest tests/core/test_settings.py::TestSteamPrefillSettings -q` → FAIL.
- [ ] **Step 3: implement** — add to `Settings` (near the steam paths):
```python
    steam_prefill_binary: Path = Path("/SteamPrefill/SteamPrefill")
    steam_prefill_config_dir: Path = Path("/SteamPrefill/Config")
```
- [ ] **Step 4: run** → PASS; full `test_settings.py` → no regressions.

---

### Task 3 — `SteamPrefillDriver`

**Files:** Create `platform/steam/prefill_driver.py`; Test `tests/platform/steam/test_prefill_driver.py`.

- [ ] **Step 1: failing test** — mock the binary (a fake `SteamPrefill` shell script echoing canned `--no-ansi` output) + sample config files in a tmp config dir:
```python
import json, stat
import pytest
from orchestrator.platform.steam.prefill_driver import SteamPrefillDriver

def _fake_binary(tmp_path, stdout="Done.", code=0):
    p = tmp_path / "FakeSteamPrefill"
    p.write_text(f'#!/bin/sh\ncat <<EOF\n{stdout}\nEOF\nexit {code}\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p

@pytest.mark.asyncio
async def test_prefill_apps_writes_selection_and_runs(tmp_path):
    cfg = tmp_path / "Config"; cfg.mkdir()
    d = SteamPrefillDriver(binary=_fake_binary(tmp_path), config_dir=cfg)
    res = await d.prefill_apps([730, 440], force=True)
    assert json.loads((cfg / "selectedAppsToPrefill.json").read_text()) == [730, 440]
    assert res.ok is True

def test_downloaded_state_parses(tmp_path):
    cfg = tmp_path / "Config"; cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text('{"730":[111,222],"440":[333]}')
    d = SteamPrefillDriver(binary=tmp_path / "x", config_dir=cfg)
    assert d.downloaded_state() == {730: [111, 222], 440: [333]}

def test_auth_status_missing_config_needs_reauth(tmp_path):
    cfg = tmp_path / "Config"; cfg.mkdir()
    d = SteamPrefillDriver(binary=tmp_path / "x", config_dir=cfg)
    assert d.auth_status().ok is False  # no account.config -> needs_reauth
```
- [ ] **Step 2: run** → FAIL (no module).
- [ ] **Step 3: implement** `platform/steam/prefill_driver.py`:
```python
from __future__ import annotations
import asyncio, json
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class PrefillResult:
    ok: bool
    raw: str

@dataclass(frozen=True)
class SteamAuthStatus:
    ok: bool
    reason: str = ""

class SteamPrefillDriver:
    """Drives the SteamPrefill binary for Steam prefill + reads its state/auth.
    Targets specific apps by writing selectedAppsToPrefill.json (no --app flag).
    Coordinates with Karl's cron via a lock + selection snapshot/restore."""

    def __init__(self, *, binary: Path, config_dir: Path) -> None:
        self._binary = Path(binary)
        self._config_dir = Path(config_dir)

    @property
    def _selection_path(self) -> Path:
        return self._config_dir / "selectedAppsToPrefill.json"

    async def prefill_apps(self, app_ids: list[int], *, force: bool = False) -> PrefillResult:
        # snapshot the operator's selection, set ours, run, restore
        prior = self._selection_path.read_text() if self._selection_path.exists() else None
        self._selection_path.write_text(json.dumps([int(a) for a in app_ids]))
        try:
            args = [str(self._binary), "prefill", "--no-ansi"] + (["--force"] if force else [])
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await proc.communicate()
            raw = out.decode("utf-8", "replace")
            return PrefillResult(ok=(proc.returncode == 0), raw=raw[-4000:])
        finally:
            if prior is not None:
                self._selection_path.write_text(prior)

    def downloaded_state(self) -> dict[int, list[int]]:
        p = self._config_dir / "successfullyDownloadedDepots.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
        return {int(k): [int(g) for g in v] for k, v in data.items()}

    def auth_status(self) -> SteamAuthStatus:
        cfg = self._config_dir / "account.config"
        if not cfg.exists():
            return SteamAuthStatus(ok=False, reason="no_account_config")
        # Pragmatic: account.config present ⇒ SteamPrefill is/has been authed (its
        # ~6-month token; SteamPrefill itself re-auths when it lapses). A precise
        # JWT-exp parse of the ProtoBuf blob is a follow-up refinement.
        return SteamAuthStatus(ok=True)
```
(`list_owned()` is added in Task 4 where library_sync needs it — implement then, parsing `select-apps status` output / SteamPrefill's owned cache.)
- [ ] **Step 4: run** → PASS; `ruff check` clean.

---

### Task 4 — Rewire prefill + library_sync + F8 + /health to SteamPrefill

**Files:** `jobs/handlers/prefill.py`, `library_sync.py`; `jobs/worker.py` (Deps — add `prefill_driver`); the `/health` Steam-status surface; F8 version-diff (in `scheduler/jobs.py` or the prefill handler). Tests alongside.

- [ ] **Step 1: failing tests** — the Steam path of the prefill handler calls `deps.prefill_driver.prefill_apps([app_id], force=...)` (driver mocked) instead of `steam_client.manifest_expand` + `prefill_chunks`; library_sync(steam) populates games from `driver.list_owned()`; F8 diff reads `driver.downloaded_state()`; `/health` Steam status reflects `driver.auth_status()`.
- [ ] **Step 2: run** → FAIL.
- [ ] **Step 3: implement**
  - **`jobs/worker.py` `Deps`:** add `prefill_driver: SteamPrefillDriver` (constructed in `api/main.py` lifespan from `settings.steam_prefill_binary`/`steam_prefill_config_dir`). Keep `steam_client` (validate still needs it).
  - **`prefill.py` Steam path:** replace the `manifest_expand` + `prefill_chunks` block with `await deps.prefill_driver.prefill_apps([steam_app_id], force=force)`; map the result to the job outcome + `games.status` (downloading→up_to_date on ok). Leave the Epic path untouched.
  - **`library_sync.py` Steam path:** source owned apps from `deps.prefill_driver.list_owned()` (implement `list_owned()` on the driver now: run `SteamPrefill select-apps status` / read its owned cache; return `[{app_id, name}]`). Upsert as today.
  - **F8 version-diff (Steam):** use `deps.prefill_driver.downloaded_state()` (app→prefilled manifest GIDs) as the "what's current" source instead of the orchestrator's own manifest-diff.
  - **`/health`:** Steam status from `deps.prefill_driver.auth_status()` (ok / needs_reauth). (Validate/worker auth is a separate internal signal; the operator-facing Steam status is SteamPrefill's.)
- [ ] **Step 4: run** → PASS; `python -m pytest tests/jobs tests/api -q` → no regressions (validate/manifest_fetch/worker paths untouched).

---

### Task 5 — Verify + commit + push + PR

- [ ] **Step 1:** `python -m pytest -q 2>&1 | tail -15` (pip-licenses env-failure aside) + `ruff check src tests` → clean.
- [ ] **Step 2:** present A/B/C, WAIT, then single `feat` commit:
```bash
git add -A && git commit -m "feat(steam): prefill + enumerate via SteamPrefill

- SteamPrefillDriver: prefill_apps (writes selectedAppsToPrefill.json + runs
  SteamPrefill prefill --no-ansi, cron-safe snapshot/restore), downloaded_state,
  auth_status (account.config), list_owned
- prefill Steam path -> SteamPrefill (modern persistent auth; bulk auth-cascade FIXED)
- library_sync + F8 version-diff + /health Steam status -> SteamPrefill
- worker/validate/manifest_fetch + Steam auth KEPT (validate manifests); full
  worker deletion + modern validate-manifest source = follow-up (gate: SteamPrefill
  manifests are SteamKit2 format, not ValvePython-parseable)

Re-architecture step (1/4).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
- [ ] **Step 3:** `git push -u origin feat/steam-via-prefill`; `gh pr create` (summary: prefill+enumerate delegated to SteamPrefill, bulk cascade fixed; worker kept for validate; **deploy must mount `/SteamPrefill` + `Config` into the orchestrator container**; re-auth is now SteamPrefill's job; follow-up for validate-manifest + worker deletion).
- [ ] **Step 4:** report PR URL + the deploy mounts needed + the named follow-up (modern validate-manifest source → delete worker).

---

## Self-Review
- **Spec coverage (re-scoped):** driver ops → Task 3; prefill/enumerate/F8/health rewire → Task 4; settings → Task 2; gate → Task 1 (done). Deletions/auth-removal/validate-via-SteamPrefill are **explicitly deferred** (spec RE-SCOPE banner) — no tasks, by design.
- **Placeholder scan:** none — code complete; `list_owned()` parsing is "read SteamPrefill select-apps status output," implemented in Task 4 against the real command (a subagent runs it live to capture the format, or mocks it).
- **Type consistency:** `SteamPrefillDriver(binary, config_dir)` + `prefill_apps/downloaded_state/auth_status/list_owned`, `PrefillResult.ok`, `SteamAuthStatus.ok` consistent Tasks 3/4; `Deps.prefill_driver` added alongside the kept `Deps.steam_client`.
- **Worker untouched:** validate, manifest_fetch, the steam worker, and Steam auth endpoints are NOT modified — confirmed no task touches them.
- **Deploy flagged:** the container/host mount of `/SteamPrefill` is a deploy-time requirement (Task 5 Step 3), not a code blocker (binary mocked in tests).
