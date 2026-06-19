# Sub-project ① — Steam via SteamPrefill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. **Task 1 is a LIVE operator-collaborative gate — run by the controller WITH Karl on the box, not a subagent — and it GATES Tasks 2+ (especially the worker deletion).**

**Goal:** Replace the orchestrator's fragile ValvePython/steam worker with a wrapper around the already-installed, modern, persistent-auth SteamPrefill — fixing the auth cascade and deleting the worker, in-place on the UGREEN.

**Architecture:** A `SteamPrefillDriver` drives the SteamPrefill binary (prefill) + reads its state/auth files; manifests are sourced from SteamPrefill's cache and parsed by a kept **parser-only** slice of the steam lib (`steam.core.manifest`, no worker/auth/gevent); F7 validate disk-stat is unchanged; the steam worker + our downloader are deleted.

**Tech Stack:** Python 3.12, SteamPrefill v3.4.2 (`/SteamPrefill/SteamPrefill`, host binary), `steam.core.manifest.DepotManifest` (parser only), pytest + ruff.

**Spec:** `docs/superpowers/specs/2026-06-19-steam-via-prefill-design.md` (033fdd5). **Parent:** re-architecture north-star (PR #174).

---

## Context the engineer needs

- **Branch:** `feat/steam-via-prefill` (off main). **No per-task commits** — single `feat` commit in the final task.
- **Run tests:** `python -m pytest <path> -q` (use `.venv/bin/python` if no `python` on PATH); `ruff check src tests`.
- **The fragile thing being replaced:** `platform/steam/` worker (`worker.py`, `client.py`, `session.py`, `credentials.py`) — a ValvePython/steam 1.4.4 gevent subprocess over IPC; it does Steam auth, owned-app enumerate, manifest fetch (CDNClient), and `manifest_expand` (raw manifest → chunk list via `steam.core.manifest`).
- **How prefill + validate work TODAY (read these):**
  - `jobs/handlers/prefill.py`: builds the deduped chunk list from the **latest stored manifest per depot** (`_LATEST_PER_DEPOT_SQL`, imported from `validator/disk_stat.py`), calls `deps.steam_client.manifest_expand(row["raw"])` to expand raw → chunks, then `prefill/downloader.py:prefill_chunks` to stream-and-discard through lancache.
  - `jobs/handlers/validate.py` + `validator/disk_stat.py:validate_game`: validates a game's stored manifests against the on-disk cache (cache-key compute in `validator/cache_key.py` + disk-stat). Manifests come from the DB `manifests` table (`raw` column), populated by `jobs/handlers/manifest_fetch.py` (which uses the steam worker's CDNClient).
  - **So both rely on DB-stored manifest `raw` + `manifest_expand`.** The rework changes the *source* of `raw` (worker → SteamPrefill cache) and makes `manifest_expand` a **direct** `steam.core.manifest` call (no worker).
- **SteamPrefill facts (recon, on the box):** binary `/SteamPrefill/SteamPrefill` v3.4.2; config `/SteamPrefill/Config/{account.config (ProtoBuf: username + JWT refresh token), selectedAppsToPrefill.json (JSON list of app-id ints), successfullyDownloadedDepots.json ({app_id_str: [manifest_gid_ints]})}`; manifest cache `~/.cache/SteamPrefill/v1/` (root cron → `/root/.cache/SteamPrefill/v1/`); `prefill` flags `--all/--recent/--recently-purchased/--top[N]/-f|--force/--os/--verbose/--no-ansi` (no `--app`); `clear-temp` purges manifests.
- **DEPLOYMENT CONSTRAINT (key):** SteamPrefill is a **host** binary; the orchestrator runs **in a container** (host networking, separate FS namespace). The driver must invoke `/SteamPrefill/SteamPrefill` + read its Config + manifest cache. Task 1 resolves the mechanism (mount + run the self-contained binary in the container, vs a host-exec). It must also **not collide with Karl's root cron** that uses the same `Config/`.
- **enforce-context7:** editing a file importing `steam.core.manifest` — already researched (`/valvepython/steam`); if blocked, `resolve-library-id` + `query-docs` first.

## File structure

- **Create** `platform/steam/prefill_driver.py` — `SteamPrefillDriver` (prefill / state / auth / enumerate).
- **Create** `platform/steam/manifest_parse.py` — thin wrapper over `steam.core.manifest.DepotManifest` (raw bytes → chunk list), replacing the worker's `manifest_expand`.
- **Modify** `core/settings.py` — `steam_prefill_binary`, `steam_prefill_config_dir`, `steam_prefill_manifest_cache_dir`.
- **Modify** `jobs/handlers/prefill.py`, `library_sync.py`, `manifest_fetch.py`, `validate.py`; `jobs/worker.py` (Deps) — rewire off `steam_client`.
- **Modify** the steam auth API router + `cli/commands/auth.py` — remove Steam auth; `/health` Steam status from `account.config`.
- **Delete** (gated on Task 1) `platform/steam/{worker,client,session,credentials}.py`, `prefill/downloader.py`, steam-worker venv + `requirements-steam-worker.txt` (keep a minimal `steam` for `steam.core.manifest`).
- Tests alongside; deploy recipe (mounts) in the final task.

---

### Task 1 — GATE (LIVE, operator-collaborative; controller + Karl; NOT code/subagent)

Resolves the three feasibility unknowns that gate everything. **If any leg fails, STOP and adjust scope before building** (esp. don't delete the worker).

- [ ] **Leg A — manifest parse.** Trigger a small SteamPrefill prefill of one app on the box, then locate its cached manifest(s) and parse one with `steam.core.manifest.DepotManifest`:
```
ssh karl@192.168.1.40
sudo /SteamPrefill/SteamPrefill prefill --no-ansi --force   # or select one app first
sudo ls -la /root/.cache/SteamPrefill/v1/                   # confirm manifests are RETAINED + per-depot identifiable
# parse one with the kept parser (worker venv has the steam lib):
docker exec orchestrator /opt/orchestrator/venv-steam-worker/bin/python - <<'PY'
from steam.core.manifest import DepotManifest
raw = open("/path/to/one/cached.manifest","rb").read()
m = DepotManifest(raw)
print("depot:", m.depot_id, "chunks:", len(list(m.payload.mappings)) if hasattr(m,'payload') else '?')
PY
```
Expected PASS: a manifest file exists, is identifiable per depot, and `DepotManifest(raw)` yields a chunk list. **Record the on-disk manifest path pattern + the raw-bytes shape** (so Task 4 can read+store them).

- [ ] **Leg B — `steam.core.manifest` imports standalone in the orchestrator venv** (no gevent monkey-patch / no worker):
```
docker exec orchestrator /app/.venv/bin/python -c "from steam.core.manifest import DepotManifest; print('ok')"
```
Expected PASS: prints `ok`. If it fails (pulls in gevent/full client), the parser must stay in a minimal isolated venv or be vendored — record which.

- [ ] **Leg C — container→host invocation + cron isolation.** Decide how the containerized orchestrator runs SteamPrefill without colliding with Karl's root cron:
```
# Is the binary self-contained (runnable in the container if mounted)?
docker exec orchestrator sh -c '/SteamPrefill/SteamPrefill --version' 2>&1 | head   # only works if /SteamPrefill is mounted; if not mounted -> mount it
# Does SteamPrefill accept an isolated config/cache dir (to avoid clobbering the cron's Config)?
/SteamPrefill/SteamPrefill --help ; /SteamPrefill/SteamPrefill prefill --help   # look for a --config / data-dir option
```
Resolve + record ONE of: (i) mount `/SteamPrefill` + a **dedicated orchestrator config/cache dir** into the container and run the self-contained binary there; (ii) if no isolated-config option exists, the orchestrator coordinates with the cron via a lock + snapshots/restores `selectedAppsToPrefill.json`; (iii) if the binary won't run in the container, a minimal host-side exec (the seed of step ②'s agent). **This decision shapes Task 3's invocation + the deploy mounts.**

- [ ] **Gate outcome.** Write the resolved facts (manifest path pattern, parser import result, invocation mechanism, cron-isolation approach) into the plan's working notes. **PASS on A+B → validate stays disk-stat fed by SteamPrefill manifests + the worker can be deleted (Task 7).** FAIL on A or B → keep a minimal manifest source / reframe validate (follow-up) and DO NOT delete the worker (skip Task 7, narrow scope). Leg C must yield a workable invocation before Task 3.

---

### Task 2 — Settings + parser dep

**Files:** `core/settings.py`; `tests/core/test_settings.py`; deps.

- [ ] **Step 1: failing test** — append:
```python
class TestSteamPrefillSettings:
    def test_defaults(self):
        from pathlib import Path
        s = Settings(orchestrator_token="t" * 32)
        assert s.steam_prefill_binary == Path("/SteamPrefill/SteamPrefill")
        assert s.steam_prefill_config_dir == Path("/SteamPrefill/Config")
        assert s.steam_prefill_manifest_cache_dir == Path("/root/.cache/SteamPrefill/v1")
```
- [ ] **Step 2: run** `python -m pytest tests/core/test_settings.py::TestSteamPrefillSettings -q` → FAIL.
- [ ] **Step 3: implement** — add to `Settings` (near the steam paths):
```python
    steam_prefill_binary: Path = Path("/SteamPrefill/SteamPrefill")
    steam_prefill_config_dir: Path = Path("/SteamPrefill/Config")
    steam_prefill_manifest_cache_dir: Path = Path("/root/.cache/SteamPrefill/v1")
```
(Use the path pattern Leg C settled on if it differs — e.g. a dedicated orchestrator config dir.) Ensure `steam.core.manifest` is importable per Leg B (keep a minimal `steam` dep; trim `requirements-steam-worker.txt` to the parser in Task 7).
- [ ] **Step 4: run** the test → PASS; `python -m pytest tests/core/test_settings.py -q` → no regressions.

---

### Task 3 — `SteamPrefillDriver`

**Files:** Create `platform/steam/prefill_driver.py`; Test `tests/platform/steam/test_prefill_driver.py`.

- [ ] **Step 1: failing test** — drive a MOCKED binary (a fake `SteamPrefill` shell script written to tmp that echoes canned `--no-ansi` output + exit code) + sample config files in a tmp config dir:
```python
import json, os, stat
import pytest
from pathlib import Path
from orchestrator.platform.steam.prefill_driver import SteamPrefillDriver

def _fake_binary(tmp_path, stdout="", code=0):
    p = tmp_path / "FakeSteamPrefill"
    p.write_text(f'#!/bin/sh\ncat <<EOF\n{stdout}\nEOF\nexit {code}\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p

@pytest.mark.asyncio
async def test_prefill_apps_writes_selection_and_runs(tmp_path):
    cfg = tmp_path / "Config"; cfg.mkdir()
    d = SteamPrefillDriver(binary=_fake_binary(tmp_path, "Done."), config_dir=cfg, manifest_cache_dir=tmp_path)
    res = await d.prefill_apps([730, 440], force=True)
    assert json.loads((cfg / "selectedAppsToPrefill.json").read_text()) == [730, 440]
    assert res.ok is True

def test_downloaded_state_parses(tmp_path):
    cfg = tmp_path / "Config"; cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text('{"730":[111,222],"440":[333]}')
    d = SteamPrefillDriver(binary=tmp_path/"x", config_dir=cfg, manifest_cache_dir=tmp_path)
    assert d.downloaded_state() == {730: [111, 222], 440: [333]}

def test_auth_status_reads_token_exp(tmp_path):
    cfg = tmp_path / "Config"; cfg.mkdir()
    # account.config is ProtoBuf; the driver extracts the embedded JWT (.ey... segment) and reads exp.
    # Use a real-ish sample captured in Task 1, or a crafted blob containing a JWT with a future exp.
    (cfg / "account.config").write_bytes(<sample bytes with a future-exp JWT>)
    d = SteamPrefillDriver(binary=tmp_path/"x", config_dir=cfg, manifest_cache_dir=tmp_path)
    assert d.auth_status().ok is True
```
(For `test_auth_status`, capture a redacted sample `account.config` byte pattern from Task 1 — or, if ProtoBuf extraction proves brittle, use the **pragmatic fallback** decided in Task 1: `account.config` exists + mtime within N days ⇒ ok. Encode whichever the driver implements.)
- [ ] **Step 2: run** → FAIL (no module).
- [ ] **Step 3: implement** `SteamPrefillDriver` with: `__init__(binary, config_dir, manifest_cache_dir)`; `async prefill_apps(app_ids: list[int], *, force=False) -> PrefillResult` (write `selectedAppsToPrefill.json`, `asyncio.create_subprocess_exec(binary, "prefill", "--no-ansi", *(["--force"] if force else []))`, capture+parse stdout for per-app success/failure, return `PrefillResult(ok, apps_ok, apps_failed, raw)`); `downloaded_state() -> dict[int, list[int]]` (json-load + int-key); `auth_status() -> SteamAuthStatus` (extract the JWT from `account.config` + check `exp`, or the mtime fallback); `list_owned() -> list[OwnedApp]` (run `select-apps status` / parse, or read SteamPrefill's owned cache — implement per what Leg A/C exposed). Cron-isolation per Leg C (lock / dedicated config). **Never log account identifiers or token bytes.**
- [ ] **Step 4: run** → PASS; `ruff check` clean.

---

### Task 4 — Manifest source = SteamPrefill cache; `manifest_parse` (direct, no worker)

**Files:** Create `platform/steam/manifest_parse.py`; Modify `jobs/handlers/manifest_fetch.py`; Tests.

- [ ] **Step 1: failing test** — `manifest_parse.expand(raw: bytes) -> list[ChunkRef]` parses a sample manifest (captured in Task 1) into the same chunk structure the worker's `manifest_expand` produced (assert chunk count + a known SHA), and `read_cached_manifests(cache_dir, app_id) -> list[(depot_id, version, raw)]` finds the app's cached manifest files.
- [ ] **Step 2: run** → FAIL.
- [ ] **Step 3: implement** `manifest_parse.expand` (wrap `steam.core.manifest.DepotManifest`, return the chunk list in the shape `prefill.py`/`disk_stat.py` expect — match the worker's `manifest_expand` output exactly) + `read_cached_manifests` (per Leg A's path pattern). Rewire `jobs/handlers/manifest_fetch.py` to read SteamPrefill's cached manifests + store `raw` in the DB `manifests` table (same schema as today), instead of the worker CDNClient.
- [ ] **Step 4: run** → PASS. Regress the existing validator tests against `manifest_parse.expand` (same results as the worker path).

---

### Task 5 — Rewire jobs off `steam_client`

**Files:** `jobs/handlers/prefill.py`, `library_sync.py`, `validate.py`, `sweep.py`; `jobs/worker.py` (Deps); Tests.

- [ ] **Step 1: failing test** — the prefill handler builds its chunk list via `manifest_parse.expand` (not `deps.steam_client.manifest_expand`) and triggers `SteamPrefillDriver.prefill_apps` for the Steam path; library_sync(steam) calls `driver.list_owned`; F8 version-diff reads `driver.downloaded_state`. (Mock the driver.)
- [ ] **Step 2: run** → FAIL.
- [ ] **Step 3: implement** — replace `deps.steam_client` usages: `prefill.py` Steam path → `driver.prefill_apps([app_id], force=...)` (SteamPrefill does the actual download-through-lancache; the orchestrator no longer streams chunks itself for Steam); `manifest_expand` → `manifest_parse.expand`; `library_sync.py` Steam → `driver.list_owned`; `validate.py` unchanged except its manifest expand uses `manifest_parse`; F8 diff uses `driver.downloaded_state`. Add `prefill_driver` to `Deps` (replacing `steam_client`).
- [ ] **Step 4: run** → PASS; `python -m pytest tests/jobs -q` no regressions.

---

### Task 6 — Remove Steam auth; /health from `account.config`

**Files:** the steam auth API router; `cli/commands/auth.py`; `api/dependencies.py` (OQ2 patterns); `/health` surface; Tests.

- [ ] **Step 1: failing test** — `POST /api/v1/platforms/steam/auth` (+ the 2FA subpath) returns 404 (removed); `/health` reports Steam auth from `SteamPrefillDriver.auth_status()` (mock ok / needs_reauth).
- [ ] **Step 2: run** → FAIL.
- [ ] **Step 3: implement** — delete the steam-auth routes + the `LOOPBACK_ONLY_PATTERNS` entries for `platforms/steam/auth`; delete `auth_steam` from the CLI; `/health` + platforms-auth-status read `driver.auth_status()`. (Epic auth untouched.)
- [ ] **Step 4: run** → PASS; `python -m pytest tests/api -q` (update/remove the steam-auth-router tests).

---

### Task 7 — Delete the worker stack (GATED on Task 1 A+B PASS)

**Files:** delete `platform/steam/{worker,client,session,credentials}.py`, `prefill/downloader.py`; trim `requirements-steam-worker.txt` to the `steam.core.manifest` parser (or vendor it); remove the worker spawn from `api/main.py` lifespan + `Deps`.

- [ ] **Step 1:** confirm no remaining imports of the deleted modules: `grep -rn "platform.steam.worker\|platform.steam.client\|steam_client\|prefill.downloader" src/orchestrator | grep -v prefill_driver`.
- [ ] **Step 2:** delete the files + the lifespan steam-worker startup/shutdown + `SteamWorkerClient` from `Deps`.
- [ ] **Step 3:** trim the steam dep to the parser; update the Dockerfile (drop the worker venv if the parser imports in the main venv per Leg B; else keep a minimal parser venv) + add the SteamPrefill mounts per Leg C.
- [ ] **Step 4:** `python -m pytest -q` full suite → green (remove dead steam-worker tests `tests/platform/steam/test_client_unit.py`, `test_worker_audit.py`, `tests/integration/test_steam_client_subprocess.py`).
- [ ] **Note:** if Task 1 FAILED A/B, SKIP this task — keep a minimal worker for manifests only and record the follow-up.

---

### Task 8 — Verify + commit + push + PR

- [ ] **Step 1:** `python -m pytest -q 2>&1 | tail -15` (pip-licenses env-failure aside) + `ruff check src tests` → clean.
- [ ] **Step 2:** present A/B/C, WAIT, then single `feat` commit:
```bash
git add -A && git commit -m "feat(steam): drive SteamPrefill; delete ValvePython/steam worker

- SteamPrefillDriver: prefill via SteamPrefill (selectedAppsToPrefill.json + prefill --no-ansi),
  downloaded_state, auth_status (account.config), list_owned
- manifests sourced from SteamPrefill cache + parsed by steam.core.manifest (parser-only)
- validate disk-stat unchanged; F8 diff uses SteamPrefill state
- removed Steam auth (endpoints/CLI/worker); /health Steam status from account.config
- DELETED the steam worker stack + our Steam downloader; deploy mounts /SteamPrefill

Re-architecture step (1/4). Steam auth now persists like SteamPrefill.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
- [ ] **Step 3:** `git push -u origin feat/steam-via-prefill` (off main — allowed). `gh pr create` (summary: the cascade fix, SteamPrefill delegation, the worker deletion, the SteamPrefill mounts the deploy needs, and that re-auth is now SteamPrefill's job).
- [ ] **Step 4:** report PR URL + the deploy change (container must mount `/SteamPrefill` + the manifest cache per Leg C), and that this completes re-architecture step ①.

---

## Self-Review

- **Spec coverage:** §3.1 driver ops → Task 3; §3.2 auth-delegation → Task 6; §3.3 validate via SteamPrefill manifests + parser → Tasks 1(gate)/4; §3.4 jobs rewire → Task 5; §4 deletions → Task 7 (gated); §5 security (no creds in orchestrator, validate selection) → Tasks 3/6; §10 gate → Task 1. The container/host constraint (spec §3.5 / deploy) → Task 1 Leg C + Task 7 Step 3.
- **Placeholder scan:** the `<sample bytes>` in Task 3's auth test + the manifest path pattern are explicitly captured in Task 1 (live) — they're gate outputs feeding later tasks, not TBDs. Leg C's invocation decision is a real fork resolved live with the exact commands to resolve it.
- **Type consistency:** `SteamPrefillDriver(binary, config_dir, manifest_cache_dir)` + `prefill_apps/downloaded_state/auth_status/list_owned` consistent across Tasks 3/5; `manifest_parse.expand(raw)->chunks` matches the worker's `manifest_expand` output (Task 4 regresses this); `Deps.prefill_driver` replaces `Deps.steam_client` consistently (Tasks 5/7).
- **Gate integrity:** Task 1 A+B PASS gates Task 7 (deletion); Leg C gates Task 3's invocation. A FAIL narrows scope (keep a minimal worker) rather than blindly deleting — stated in Task 1 + Task 7.
- **Risk honestly flagged:** the container/host boundary + cron collision is the genuine hard part; Task 1 Leg C resolves it before code, and it's the natural seed of step ②'s data-plane agent.
