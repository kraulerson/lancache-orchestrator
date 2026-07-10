# Manual-Coverage Orchestrator Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the orchestrator serve manual-download folder listings for launchers whose on-disk folder name contains a space or dot (`Amazon Games`, `Humble Bundle`, `Itch.io`), and optionally list loose files (Humble/Itch), so Game_shelf can diff them against the owned library.

**Architecture:** Two tiny, backward-compatible changes mirrored across the agent router (`GET /v1/manual-downloads/{launcher}`), the control-plane proxy (`GET /api/v1/manual-downloads/{launcher}`), and the client (`AgentClient.manual_downloads`). (1) Widen the launcher-name allowlist regex from `^[A-Za-z0-9_-]+$` to `^[A-Za-z0-9 ._-]+$` — traversal stays impossible (no `/`; the resolve-under-root guard rejects `.`/`..`). (2) Add an `include_files` query param (default `false`) so file-based launchers can list files, not just directories; default `false` keeps GOG/Amazon behavior byte-identical.

**Tech Stack:** Python 3.12, FastAPI, pytest, mypy, ruff. Framework hooks active (enforce-plan-tracking → a plan task must be `in_progress` before editing source; enforce-evaluate → marker before each commit; pre-commit ruff+mypy+semgrep).

## Global Constraints

- Regex (verbatim, both routers): `^[A-Za-z0-9 ._-]+$`. No `/`, no other chars.
- The resolve-under-root guard (`if target.parent != root: 400`) MUST remain in the agent router.
- `include_files` default is `False` everywhere; GOG/Amazon must keep dir-only listing.
- Agent still skips entries whose name starts with `!` or `.` in BOTH dir and file mode.
- `AgentClient.manual_downloads` MUST URL-encode the launcher: `urllib.parse.quote(launcher, safe="")`.
- No schema/migration. No new third-party imports (`urllib.parse` is stdlib — no Context7 lookup needed).
- Run tests from repo root with `.venv/bin/python -m pytest ... -q`. Commit only after ruff+mypy clean.

---

### Task 1: Agent router — widen regex + `include_files`

**Files:**
- Modify: `src/orchestrator/agent/routers/manual_downloads.py`
- Test: `tests/agent/test_manual_downloads.py`

**Interfaces:**
- Produces: `GET /v1/manual-downloads/{launcher}?include_files=<bool>` — 200 `{launcher, present, entries}`; accepts space/dot launcher names; `include_files=true` adds regular files to `entries`.

- [ ] **Step 1: Write failing tests** (append to `tests/agent/test_manual_downloads.py`)

```python
def test_accepts_launcher_with_space_and_dot(tmp_path):
    for folder in ("Amazon Games", "Humble Bundle", "Itch.io"):
        (tmp_path / folder / "A Game").mkdir(parents=True)
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers=AUTH)
    for folder in ("Amazon Games", "Humble Bundle", "Itch.io"):
        r = client.get(f"/v1/manual-downloads/{folder}")
        assert r.status_code == 200, folder
        assert r.json()["entries"] == ["A Game"]


def test_include_files_lists_files_when_true(tmp_path):
    hb = tmp_path / "Humble Bundle"
    hb.mkdir(parents=True)
    (hb / "VVVVVV-04212026.zip").write_text("x")
    (hb / "A Folder Game").mkdir()
    (hb / "!downloading").write_text("x")
    (hb / ".hidden").write_text("x")
    app = create_agent_app(settings=_settings(tmp_path))
    client = TestClient(app, headers=AUTH)
    default = client.get("/v1/manual-downloads/Humble Bundle").json()["entries"]
    assert default == ["A Folder Game"]  # dirs only, unchanged
    withfiles = client.get("/v1/manual-downloads/Humble Bundle?include_files=true").json()["entries"]
    assert withfiles == ["A Folder Game", "VVVVVV-04212026.zip"]  # file added, !/. skipped
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/agent/test_manual_downloads.py -q`
Expected: FAIL (space launcher → 400; `include_files` param ignored).

- [ ] **Step 3: Implement** — edit `src/orchestrator/agent/routers/manual_downloads.py`

Change the regex constant:
```python
_LAUNCHER_RE = re.compile(r"^[A-Za-z0-9 ._-]+$")
```
Change the handler signature + listing to accept `include_files`:
```python
@router.get("/v1/manual-downloads/{launcher}")
async def manual_downloads(
    launcher: str, request: Request, include_files: bool = False
) -> ManualDownloadsResponse:
    if not _LAUNCHER_RE.match(launcher):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid launcher")
    settings = request.app.state.settings
    root = settings.manual_downloads_cache_path.resolve()
    target = (root / launcher).resolve()
    if target.parent != root:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid launcher")
    if not target.is_dir():
        return ManualDownloadsResponse(launcher=launcher, present=False, entries=[])
    entries = sorted(
        e.name
        for e in target.iterdir()
        if (e.is_dir() or (include_files and e.is_file()))
        and not e.name.startswith(("!", "."))
    )
    return ManualDownloadsResponse(launcher=launcher, present=True, entries=entries)
```

- [ ] **Step 4: Run to verify pass** (existing traversal test `test_rejects_path_traversal_launcher` must still pass — `..`/`GOG/..` still rejected by the resolve guard; `%2e%2e` decodes to `..`)

Run: `.venv/bin/python -m pytest tests/agent/test_manual_downloads.py -q`
Expected: PASS (all).

- [ ] **Step 5: ruff + mypy, then commit**

Run: `.venv/bin/ruff check src/orchestrator/agent/routers/manual_downloads.py && .venv/bin/mypy src/orchestrator/agent/routers/manual_downloads.py`
```bash
git add src/orchestrator/agent/routers/manual_downloads.py tests/agent/test_manual_downloads.py
git commit -m "feat(#222): agent manual-downloads accepts space/dot launchers + include_files"
```

---

### Task 2: `AgentClient.manual_downloads` — encode launcher + `include_files`

**Files:**
- Modify: `src/orchestrator/clients/agent_client.py` (method at ~line 245)
- Test: `tests/clients/test_agent_client.py` (create if absent; otherwise append)

**Interfaces:**
- Consumes: agent endpoint from Task 1.
- Produces: `AgentClient.manual_downloads(launcher: str, include_files: bool = False) -> dict[str, Any]` — issues `GET /v1/manual-downloads/<quoted-launcher>[?include_files=true]`.

- [ ] **Step 1: Write failing test** — capture the path the client requests. Look at how other tests in this repo fake `_request` (e.g. `tests/clients/`), then:

```python
import pytest
from orchestrator.clients.agent_client import AgentClient


class _CapResp:
    def json(self):
        return {"launcher": "Amazon Games", "present": True, "entries": []}


@pytest.mark.asyncio
async def test_manual_downloads_encodes_launcher_and_include_files(monkeypatch):
    client = AgentClient(base_url="http://agent", token="t")
    seen = {}

    async def fake_request(method, path, **kw):
        seen["method"], seen["path"] = method, path
        return _CapResp()

    monkeypatch.setattr(client, "_request", fake_request)
    await client.manual_downloads("Amazon Games")
    assert seen["path"] == "/v1/manual-downloads/Amazon%20Games"
    await client.manual_downloads("Itch.io", include_files=True)
    assert seen["path"] == "/v1/manual-downloads/Itch.io?include_files=true"
```
(Match the actual `AgentClient` constructor signature and async test style used elsewhere in `tests/clients/`; adjust `AgentClient(...)` args to the real ones.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/clients/test_agent_client.py -q -k manual_downloads`
Expected: FAIL (space not encoded; no `include_files`).

- [ ] **Step 3: Implement** — edit `src/orchestrator/clients/agent_client.py`

Add `from urllib.parse import quote` to the imports at the top of the file, then replace the method:
```python
    async def manual_downloads(
        self, launcher: str, include_files: bool = False
    ) -> dict[str, Any]:
        """List the manually-downloaded game entries under `<cache>/<launcher>/`
        on the agent host (#222). Returns {launcher, present, entries}. The launcher
        may contain spaces/dots (e.g. "Amazon Games") so it is URL-encoded here; the
        caller still validates it against the alnum/space/./-/_ allowlist. With
        include_files, loose files (Humble/Itch installers) are listed too."""
        path = f"/v1/manual-downloads/{quote(launcher, safe='')}"
        if include_files:
            path += "?include_files=true"
        resp = await self._request("GET", path)
        result: dict[str, Any] = resp.json()
        return result
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/clients/test_agent_client.py -q -k manual_downloads`
Expected: PASS.

- [ ] **Step 5: ruff + mypy, then commit**

Run: `.venv/bin/ruff check src/orchestrator/clients/agent_client.py && .venv/bin/mypy src/orchestrator/clients/agent_client.py`
```bash
git add src/orchestrator/clients/agent_client.py tests/clients/test_agent_client.py
git commit -m "feat(#222): AgentClient.manual_downloads url-encodes launcher + include_files"
```

---

### Task 3: Control-plane router — widen regex + forward `include_files`

**Files:**
- Modify: `src/orchestrator/api/routers/manual_downloads.py`
- Test: `tests/api/test_manual_downloads_router.py`

**Interfaces:**
- Consumes: `AgentClient.manual_downloads(launcher, include_files)` from Task 2.
- Produces: `GET /api/v1/manual-downloads/{launcher}?include_files=<bool>` — accepts space/dot launchers, forwards `include_files` to the client.

- [ ] **Step 1: Write failing tests** — the existing test file defines a fake client with `manual_downloads(self, launcher)`; widen it to capture `include_files`. Add:

```python
def test_router_accepts_space_launcher_and_forwards_include_files(...):
    # Fake client records (launcher, include_files); assert:
    #  GET /api/v1/manual-downloads/Amazon%20Games         -> client called ("Amazon Games", False)
    #  GET /api/v1/manual-downloads/Itch.io?include_files=true -> client called ("Itch.io", True)
```
Update the fake client signature to `async def manual_downloads(self, launcher, include_files=False)` and record both args. (Follow the existing harness in `tests/api/test_manual_downloads_router.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/api/test_manual_downloads_router.py -q`
Expected: FAIL (space launcher rejected by old regex; `include_files` not forwarded).

- [ ] **Step 3: Implement** — edit `src/orchestrator/api/routers/manual_downloads.py`

Change the regex:
```python
_LAUNCHER_RE = re.compile(r"^[A-Za-z0-9 ._-]+$")
```
Change the handler to accept and forward `include_files`:
```python
async def manual_downloads(
    launcher: str, request: Request, include_files: bool = False
) -> JSONResponse:
    if not _LAUNCHER_RE.match(launcher):
        return JSONResponse(content={"detail": "invalid launcher"}, status_code=400)
    client = getattr(request.app.state, "agent_client", None)
    if client is None:
        return JSONResponse(content={"detail": "agent not configured"}, status_code=503)
    try:
        result = await client.manual_downloads(launcher, include_files=include_files)
    except Exception as e:
        _log.error("api.manual_downloads.agent_error", launcher=launcher, reason=str(e)[:200])
        return JSONResponse(content={"detail": "agent unavailable"}, status_code=503)
    return JSONResponse(content=result)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/api/test_manual_downloads_router.py -q`
Expected: PASS.

- [ ] **Step 5: ruff + mypy, then commit**

Run: `.venv/bin/ruff check src/orchestrator/api/routers/manual_downloads.py && .venv/bin/mypy src/orchestrator/api/routers/manual_downloads.py`
```bash
git add src/orchestrator/api/routers/manual_downloads.py tests/api/test_manual_downloads_router.py
git commit -m "feat(#222): control manual-downloads accepts space/dot launchers + forwards include_files"
```

---

### Task 4: CHANGELOG + full-suite verification + PR

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: CHANGELOG** — under the current unreleased section add:
```
### Changed
- Manual-downloads endpoint accepts launcher folder names with spaces/dots
  (Amazon Games, Humble Bundle, Itch.io) and can list loose files via
  `?include_files=true` (default off keeps GOG/Amazon dir-only). (#222)
```

- [ ] **Step 2: Full suite + lint**

Run: `.venv/bin/python -m pytest -q --ignore=tests/scripts` (only pre-existing `test_licenses.py` may fail)
Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src`
Expected: green (aside from the known pre-existing licenses failure).

- [ ] **Step 3: Commit + push + open PR**
```bash
git add CHANGELOG.md
git commit -m "docs(#222): CHANGELOG — manual-downloads space/dot launchers + include_files"
git push -u origin <branch>
gh pr create --title "feat(#222): manual-downloads support for Amazon/Humble/Itch (space/dot + files)" --body "..."
```
(Karl merges the PR — never `gh pr merge`.)

## Deploy (after merge; no 2FA)
- Control plane LXC 1105: `cd /root/lancache-orchestrator && git fetch && git reset --hard origin/main && docker build -t orchestrator:dpa . && bash /root/deploy-orchestrator-lxc.sh` (tag rollback `orchestrator:dpa-pre-manualcov` first).
- Agent (UGREEN .40): RECREATE the `orchestrator-agent` container (via `/home/karl/deploy-agent.sh`) so the router change loads — restart is not enough for a code change.
- Verify: `GET /api/v1/manual-downloads/Amazon%20Games` → 200 with 384 entries; `.../Humble%20Bundle?include_files=true` → the 18 files.

## Self-Review
- **Spec coverage:** A1 regex (Tasks 1+3), A2 include_files (Tasks 1+3), agent_client encode (Task 2). ✓
- **Traversal:** resolve-under-root guard retained in Task 1; existing traversal test unchanged. ✓
- **Regression:** default `include_files=false` → dir-only, asserted in Task 1. ✓
