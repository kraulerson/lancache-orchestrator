# Independent Steam Manifest Fetcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A one-Steam-login, manifests-only fetcher (ValvePython asyncio) that writes the full owned library's depot manifests into the durable archive so the F7 validator covers everything — no per-app login rate-limit, no chunk re-download.

**Architecture:** A new agent module logs in once from a persisted session, enumerates owned apps, and for each depot fetches the manifest (chunk list) only via ValvePython's CDNClient, writing it into `/manifest-archive/v1/`. The existing PR #200 union-read + sweep validate it — ideally with zero validator change. A one-time interactive login (operator) persists the session; everything after is unattended.

**Tech Stack:** Python 3.12, FastAPI agent, ValvePython/steam (asyncio), click; pytest/ruff/mypy.

**Spec:** `docs/superpowers/specs/2026-06-24-steam-manifest-fetcher-design.md`

**Conventions:** TDD on Phase 1 (mocked ValvePython — NO live Steam in unit tests). **No per-task commits — ONE `feat` commit at the end** (Task 12), except Phase 0 spike findings which are committed as a `docs`/`spike` note. Before the feat commit: `.venv/bin/ruff format`, `.venv/bin/ruff check`, `.venv/bin/mypy src/orchestrator`, full suite `.venv/bin/python -m pytest -q --ignore=tests/scripts` (only acceptable failure `tests/test_licenses.py`). No `assert` in `src/` (S101 → `if … raise`); bare dict returns `dict[str, Any]`. Mark each task `in_progress` (enforce-plan-tracking) before editing. ValvePython API via Context7 `/valvepython/steam`. **SECURITY: never store/log the Steam password, Steam Guard code, shared_secret, or any token; ONE login per fetch run.**

**Spike-gating:** Phase 0 (S1–S3) MUST complete first; their findings pick the output-format branch in Task 3 and the exact ValvePython calls in Task 2. The executing controller records each finding before starting Phase 1.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `docs/superpowers/spikes/2026-06-24-fetcher-spikes.md` | S1–S3 findings | **create** |
| `src/orchestrator/core/settings.py` | `steam_fetcher_session_dir`, `steam_fetcher_request_delay_sec` | modify |
| `src/orchestrator/agent/steam_manifest_fetcher.py` | the fetcher (login + fetch_all + write) | **create** |
| `src/orchestrator/agent/steam_login.py` | one-time interactive login entrypoint | **create** |
| `src/orchestrator/agent/manifest_parser.py` | (only if S2 says) `.shas` parse branch | modify (conditional) |
| `src/orchestrator/agent/manifest_locator.py` | (only if S2 says) glob `.shas` too | modify (conditional) |
| `src/orchestrator/agent/routers/steam.py` | `POST /v1/steam/fetch-manifests` | modify |
| `src/orchestrator/agent/jobs.py` | fetch job state (reuse AgentJobStore) | modify (if needed) |
| `src/orchestrator/clients/agent_client.py` | `fetch_manifests()` control→agent call | modify |
| `src/orchestrator/api/routers/...` + `cli/commands/cache.py` | `cache fetch-manifests` trigger | modify |
| `requirements-steam-fetcher.txt` (+ Dockerfile) | pinned ValvePython for the agent image | **create/modify** |

---

## PHASE 0 — SPIKES (gate the build; record findings before Phase 1)

> These are live research, not red-green TDD. Each ends by writing its finding into `docs/superpowers/spikes/2026-06-24-fetcher-spikes.md`. They need a live Steam session — so do **S0** first.

### S0: throwaway env + one-time session (operator 2FA)

- [ ] **Step 1:** On the NAS, in a throwaway venv (not the agent image yet): `python3.12 -m venv /tmp/vpy && /tmp/vpy/bin/pip install steam` (ValvePython; record the exact resolved version). Confirm it imports under 3.12.
- [ ] **Step 2:** Write `/tmp/login.py` using Context7 `/valvepython/steam` current API to log in (`username + password + Steam Guard`) and persist the session to `/tmp/fetcher-session`. **Karl runs this once interactively** (`/tmp/vpy/bin/python /tmp/login.py`) and enters his 2FA. The script must NOT print/log the password or code. (Operator-collaborative: Karl's 2FA.)
- [ ] **Step 3:** Confirm a second run logs in **from the persisted session with no 2FA prompt**. Record the session file path/shape + the resolved ValvePython version in the spike doc.

### S1: ValvePython asyncio API (no gevent)

- [ ] **Step 1:** With the session, script: login-from-session → `get_product_info([app])` for one owned app → read depots + current `manifest` GIDs → `CDNClient.get_manifest(app, depot, gid)`. Use the **asyncio** interface (per Context7). Time it; confirm no gevent import is required.
- [ ] **Step 2:** Record in the spike doc: the exact asyncio call sequence that works (this becomes Task 2's implementation), whether gevent is pulled in transitively, and any `BaseException`/timeout types to contain. If asyncio is unavailable/unstable, document the gevent-containment approach (a `BaseException` boundary + a worker thread) for Task 2.

### S2: manifest format compatibility (picks Task 3 branch)

- [ ] **Step 1:** Serialize one fetched manifest to bytes and write it as `/tmp/{app}_{app}_{depot}_{gid}.bin`. In the repo venv run `parse_chunk_shas(Path(...).read_bytes())` (from `src/orchestrator/agent/manifest_parser.py`) and check it returns the chunk SHA1 set.
- [ ] **Step 2:** Cross-check those SHAs against the SteamPrefill `.bin` for the SAME app (from `/steamprefill-cache/v1`) — same chunk set ⇒ format-compatible.
- [ ] **Step 3:** **Decision (record it):**
  - **Compatible →** Task 3 = "emit `.bin`": the fetcher writes the serialized manifest bytes as `{app}_{app}_{depot}_{gid}.bin`. **Zero** change to `manifest_parser`/`manifest_locator`.
  - **Incompatible →** Task 3 = "`.shas` sidecar": the fetcher writes `{app}_{app}_{depot}_{gid}.shas` (one lowercase sha1 hex per line). Add a `.shas` parse path + extend `locate_manifest_bins`/`list_prefilled_app_ids` to glob `*.bin` **and** `*.shas` (keep newest-per-depot-by-mtime; a `.bin` and `.shas` for the same depot/gid de-dupe by gid).

### S3: depot/license enumeration correctness

- [ ] **Step 1:** For 2–3 owned apps, enumerate depots via `get_product_info` filtered to **OS=windows** + depots the account has a license for; compare the depot IDs + manifest GIDs to what SteamPrefill cached (`/steamprefill-cache/v1` filenames) for the same apps.
- [ ] **Step 2:** Write one fetched manifest into `/manifest-archive/v1/` and run the agent validate for that app (the `steam_validate` code path) — confirm a non-`error` outcome with a sane cached/total (proves the computed cache-key paths match on-disk chunks). Record the OS/license filter rule in the spike doc.
- [ ] **Step 3:** Commit the spike doc: `git add docs/superpowers/spikes/2026-06-24-fetcher-spikes.md && git commit -m "spike(steam-fetcher): S1–S3 findings"` (A/B/C first per commit-approval).

---

## PHASE 1 — BUILD (TDD, mocked ValvePython)

### Task 1: settings

**Files:** Modify `src/orchestrator/core/settings.py`; Test `tests/core/test_settings.py`.

- [ ] **Step 1: failing test**
```python
def test_fetcher_settings_defaults():
    s = Settings(orchestrator_token=VALID_TOKEN)
    assert str(s.steam_fetcher_session_dir) == "/steam-fetcher-session"
    assert s.steam_fetcher_request_delay_sec == 0.2
```
- [ ] **Step 2:** Run `-k fetcher_settings` → FAIL (no attribute).
- [ ] **Step 3: implement** (after `steam_manifest_archive_dir`):
```python
    # Independent Steam manifest fetcher (2026-06-24): its own persisted Steam
    # session (NOT the SteamPrefill Config) + a polite inter-request delay.
    steam_fetcher_session_dir: Path = Path("/steam-fetcher-session")
    steam_fetcher_request_delay_sec: float = Field(default=0.2, ge=0)
```
- [ ] **Step 4:** Run → PASS.

### Task 2: SteamManifestFetcher core (mocked ValvePython)

**Files:** Create `src/orchestrator/agent/steam_manifest_fetcher.py`; Test `tests/agent/test_steam_manifest_fetcher.py`.

Design (interface — internals use the exact asyncio calls recorded in S1):
- `class SteamAuthError(Exception)` — session missing/expired.
- `class FetchResult` (dataclass): `attempted: int, written: int, skipped: int, failed: int, errors: list[str]`.
- `class SteamManifestFetcher`: `__init__(self, *, session_dir: Path, archive_dir: Path, request_delay_sec: float, client=None)` (`client` injectable for tests; real one built from S1's API).
  - `async login_from_session(self) -> None` — raise `SteamAuthError` if no/expired session.
  - `async fetch_all(self, app_ids: list[int]) -> FetchResult` — one login, then per app: get depots+gids → per depot: skip if archive file exists; else `get_manifest` → write → `written+=1`; `await asyncio.sleep(request_delay_sec)`. Per-app `try/except Exception` → `failed+=1`, append short error, continue. The whole body wrapped so **`except BaseException`** logs + re-raises only after ensuring no partial corruption (the ③ lesson: a `gevent.Timeout` must not silently kill the run un-counted).
- STDLIB + ValvePython only; **no** `orchestrator.api.main`/`orchestrator.db.pool` import.

- [ ] **Step 1: failing tests** (mocked client — `FakeClient` returns canned product-info + manifest objects):
```python
async def test_fetch_writes_manifest_per_depot(tmp_path):
    fake = FakeClient(owned={440: [(441, "gidA")]}, manifests={(440,441,"gidA"): ["aa"*20, "bb"*20]})
    f = SteamManifestFetcher(session_dir=tmp_path/"s", archive_dir=tmp_path/"a",
                             request_delay_sec=0, client=fake)
    res = await f.fetch_all([440])
    assert res.written == 1
    assert (tmp_path/"a"/"v1"/"440_440_441_gidA.bin").exists()  # or .shas per S2

async def test_idempotent_skip(tmp_path): ...        # second run skips, skipped==1, written==0
async def test_per_app_error_isolated(tmp_path): ... # one app raises → failed==1, others written
async def test_login_expired_raises(tmp_path):       # FakeClient(expired=True) → SteamAuthError
```
- [ ] **Step 2:** Run → FAIL (module missing).
- [ ] **Step 3:** Implement per the interface above + the S1-recorded calls. Filename `{app}_{app}_{depot}_{gid}` + the S2 extension. Idempotent via `Path.exists()`. Inter-request `asyncio.sleep`. `BaseException` boundary.
- [ ] **Step 4:** Run → PASS. Then `.venv/bin/python -m pytest tests/agent/test_import_isolation.py -q` → still green.

### Task 3: output format wiring (per S2 decision)

**If S2 = compatible:** nothing here beyond Task 2 writing `.bin` — skip to Task 4.
**If S2 = incompatible (`.shas`):**
**Files:** Modify `src/orchestrator/agent/manifest_parser.py`, `src/orchestrator/agent/manifest_locator.py`; Test the parser + locator tests.

- [ ] **Step 1: failing tests:** a `.shas` file (`"aa"*20 + "\n" + "bb"*20`) parses to that SHA set; `locate_manifest_bins` finds an app whose only manifest is a `.shas`; a `.bin` and `.shas` for the same depot de-dupe (newest by mtime/gid).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add `parse_shas(text: str) -> set[str]` (split lines, keep 40-hex) and a `.shas` branch in the validate path; extend `locate_manifest_bins`/`list_prefilled_app_ids` globs to `("*.bin", "*.shas")`. Keep newest-per-depot-by-mtime.
- [ ] **Step 4:** Run → PASS (+ existing manifest tests still green).

### Task 4: one-time interactive login entrypoint

**Files:** Create `src/orchestrator/agent/steam_login.py`; Test `tests/agent/test_steam_login.py`.

- `def main()` (`python -m orchestrator.agent.steam_login`): prompt `username` (arg/env) + `getpass` password + Steam Guard; call ValvePython login (S1 API); on success, persist ONLY the session to `settings.steam_fetcher_session_dir`. Print success WITHOUT any secret. Password/code held in locals only.

- [ ] **Step 1: failing test** (mock the login + getpass): on success a session file is written under the session dir; **assert no written file and no captured stdout contains the password or the 2FA code**.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement with `getpass.getpass`; never `log`/`print` the secret; write only the session.
- [ ] **Step 4:** Run → PASS.

### Task 5: agent endpoint `POST /v1/steam/fetch-manifests`

**Files:** Modify `src/orchestrator/agent/routers/steam.py` (+ `agent/jobs.py` if a new job shape is needed); Test `tests/agent/test_steam.py` (or a new `test_fetch_manifests_router.py`).

- Mirror the existing agent prefill job pattern (`start_prefill`/`get_prefill` in `routers/steam.py` + `AgentJobStore`): `POST /v1/steam/fetch-manifests` body `{app_ids: list[int]}` → spawns `SteamManifestFetcher(...).fetch_all(app_ids)` as a tracked background job → `202 {job_id}`; `GET /v1/steam/fetch-manifests/{job_id}` → state/result. Bearer-gated (not in `_AGENT_EXEMPT_PATHS`).

- [ ] **Step 1: failing tests:** POST without bearer → 401; POST with bearer + `{app_ids:[440]}` (fetcher monkeypatched to a fake) → 202 + job_id; GET job → terminal state with the FetchResult.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement mirroring `start_prefill`/`get_prefill` + `_validate_app_ids`.
- [ ] **Step 4:** Run → PASS.

### Task 6: AgentClient.fetch_manifests (control→agent)

**Files:** Modify `src/orchestrator/clients/agent_client.py`; Test `tests/clients/test_agent_client.py`.

- [ ] **Step 1: failing test:** `fetch_manifests([440])` POSTs `/v1/steam/fetch-manifests`, polls to done, returns the result (mirror `steam_prefill`'s post-then-poll using `MockTransport`).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `async def fetch_manifests(self, app_ids, *, poll_timeout_sec=7200.0)` reusing the existing post-then-poll helper.
- [ ] **Step 4:** Run → PASS.

### Task 7: control-plane trigger — `cache fetch-manifests`

**Files:** Modify `src/orchestrator/cli/commands/cache.py` (+ an enqueue path). Test `tests/cli/test_cmd_cache.py`.

- Add `cache fetch-manifests`: enumerate owned steam app_ids from the games table (via the API — reuse the games list or a small endpoint) and trigger the agent fetch. Simplest: a control-plane `POST /api/v1/steam/fetch-manifests` that reads owned steam app_ids from the DB and calls `agent_client.fetch_manifests(...)` as a job; the CLI calls that endpoint.

- [ ] **Step 1: failing test:** `cache fetch-manifests` POSTs to the control-plane endpoint; endpoint enumerates owned steam apps and calls the agent (mock the agent_client) → returns a job/summary. CLI prints success.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the endpoint (bearer-gated) + the CLI command (mirror `cache validate-all` in `cli/commands/cache.py`).
- [ ] **Step 4:** Run → PASS.

### Task 8: packaging — ValvePython in the agent image

**Files:** Create `requirements-steam-fetcher.txt` (pinned exact, from S0's resolved version + transitive pins via `pip-compile`-style freeze); Modify `Dockerfile` (install it in the agent image only); Test: image builds + `python -c "import steam"` works.

- [ ] **Step 1:** Pin ValvePython + transitive deps exactly in `requirements-steam-fetcher.txt`.
- [ ] **Step 2:** Add a `pip install -r requirements-steam-fetcher.txt` layer in the Dockerfile (keep it lean — only what the fetcher needs). Note pip-audit only checks runtime `requirements.txt`; document the new file's audit handling.
- [ ] **Step 3:** Local: `python -c "import steam"` in the venv with the pinned deps imports clean.

### Task 9–11: (reserved — fold any S2/S3-driven extra wiring here; otherwise skip)

### Task 12: full verification + single feat commit (controller)

- [ ] `.venv/bin/ruff format src tests` · `.venv/bin/ruff check src tests` · `.venv/bin/mypy src/orchestrator` (clean).
- [ ] `.venv/bin/python -m pytest -q --ignore=tests/scripts` (only `tests/test_licenses.py` may fail).
- [ ] Present evaluation + commit-structure A/B/C, then ONE `feat` commit. Karl opens/merges.
```
feat(steam): independent manifest fetcher (one login, manifests-only)

Adds a ValvePython-asyncio fetcher that does ONE Steam login and writes the
owned library's depot manifests into the durable archive (no chunk re-download,
no per-app login rate-limit), so the F7 validator covers the full library —
including never-prefilled apps. One-time interactive login persists a session;
fetch + validate-all run unattended. Spec/plan + spike findings in docs/.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## OPERATOR GO-LIVE (post-merge — Claude runs the boxes; only the 2FA is Karl's)

1. Build + deploy the new agent image on the NAS; add `-v steam-fetcher-session:/steam-fetcher-session` (chown 1000) to `deploy-agent.sh`; recreate the agent.
2. **Karl runs the one-time login** (his 2FA): `docker exec -it orchestrator-agent python -m orchestrator.agent.steam_login` → username + password + Steam Guard. Session persists; never logged.
3. `orchestrator-cli cache fetch-manifests` (ONE login, all owned apps, minutes). Confirm the archive jumps to ~the full owned-app count.
4. `orchestrator-cli cache validate-all` → confirm the ~673 stable apps flip to true cache state; report before/after histogram and reconcile against Karl's "over 1000 cached" expectation.
5. Note: re-run `cache fetch-manifests` weekly (or on a schedule) to keep manifests current; the PR #200 SteamPrefill sync continues to complement it.

---

## Self-Review

**Spec coverage:** library/asyncio → S1 + Task 2; auth/one-time-login → S0 + Task 4; output format → S2 + Task 3; enumerate-all-owned → Task 7; complement-not-replace → unchanged sync + Task 2 writes the same archive; trigger → Tasks 5–7; packaging → Task 8; security (no secret persisted) → Task 4 test; gevent containment → Task 2 `BaseException` boundary + S1; import-isolation → Task 2 Step 4. All spec sections map to a task.

**Placeholder scan:** the only deliberately-deferred specifics (exact ValvePython calls, `.bin` vs `.shas`) are **resolved by S1/S2 before** the dependent tasks — that's the spike-gating, not a placeholder. Task 2/3 give the full interface + both branches.

**Type consistency:** `SteamManifestFetcher(session_dir, archive_dir, request_delay_sec, client)`, `FetchResult(attempted, written, skipped, failed, errors)`, `SteamAuthError`, `fetch_all(app_ids)`, `AgentClient.fetch_manifests(app_ids)` — consistent across Tasks 2/5/6/7.
