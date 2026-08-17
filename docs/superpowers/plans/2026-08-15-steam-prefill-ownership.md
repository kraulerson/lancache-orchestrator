# Steam Prefill Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move scheduled Steam prefill from the host cron to the orchestrator, after first making the orchestrator's prefill path safe to run unattended.

**Architecture:** SteamPrefill stays the Steam engine — this plan never re-implements Steam auth or CDN access. `SteamPrefillDriver` (which already exists and already runs the binary) gains a timeout, a process-group kill, and a `--recently-purchased` mode. The agent gains single-flight protection. The orchestrator gains its first outbound notifier and a Steam-specific scheduled job, sibling to the existing Epic one. The host cron is retired last, behind a 7-day gate.

**Tech Stack:** Python 3.12, asyncio, FastAPI, APScheduler 3.x, pydantic-settings v2, aiosqlite, pytest + pytest-asyncio, structlog.

**Spec:** `docs/superpowers/specs/2026-08-14-steam-prefill-ownership-design.md` — read §3 (findings) and §7.1 (notifier) before starting.

---

## Global Constraints

These apply to **every** task. They are not optional and they are not negotiable.

- **Interpreter:** `.venv/bin/python`. Bare `python` is NOT on PATH.
- **Run tests:** `.venv/bin/python -m pytest tests/... -q`
- **Run the FULL suite** as: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q`
  The `PATH` prefix is REQUIRED. Without it `tests/test_licenses.py` fails with
  `FileNotFoundError: 'pip-licenses'` — an environment artifact, not a real failure.
- **Lint/format/types must be clean before every commit:**
  `.venv/bin/python -m ruff check src/ tests/`
  `.venv/bin/python -m ruff format src/ tests/`
  `.venv/bin/python -m mypy src/`   (strict; 104 files today)
- **TDD is mandatory.** Write the test, RUN IT AND SEE IT FAIL, then implement. A test that
  passes the first time is proving nothing — tighten it until it fails for the right reason.
- **FRAMEWORK HOOKS — you WILL be blocked if you skip these:**
  1. Before editing ANY file under `src/`, invoke a Superpowers skill
     (`superpowers:test-driven-development` is the right one). The marker **resets after every
     commit**, so re-invoke before the next task's source edits.
  2. Before EVERY `git commit`, present the change to the human and get approval, then run
     **from the repo root, as its own command, with a relative path**:
     `.claude/framework/hooks/mark-evaluated.sh "short reason"`
     The reason must contain **no shell-special characters** — no `;`, `&`, `|`, quotes,
     apostrophes. A semicolon in that string trips config-guard and the call is rejected.
  3. Never edit anything under `.claude/` and never access it via Bash.
- **Line length 100.** Double quotes. `from __future__ import annotations` at the top of every
  new module.
- **Never log secrets.** `core/logging.py::_SENSITIVE_KEY_RE` already redacts any key containing
  `password`, `token`, `secret`, etc.
- **Commit granularity:** one commit per task, after its tests pass.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/orchestrator/platform/steam/prefill_driver.py` | Runs the SteamPrefill binary. Gains timeout, group-kill, recent-purchase mode | 1, 2 |
| `src/orchestrator/agent/routers/steam.py` | Agent HTTP surface. Gains single-flight + recent endpoint | 3 |
| `src/orchestrator/agent/app.py` | Wires the driver from settings. Passes the new timeout | 1 |
| `src/orchestrator/core/notify.py` | **NEW.** SMTP alert sender with per-key cooldown | 4 |
| `src/orchestrator/core/settings.py` | New settings: timeout, alerting, Steam schedule | 1, 4, 7 |
| `src/orchestrator/scheduler/jobs.py` | Gains `enqueue_scheduled_steam_prefill` + stall check | 6, 7 |
| `src/orchestrator/scheduler/manager.py` | Registers the new Steam job on a wall-clock cron | 7 |
| `src/orchestrator/api/main.py` | Wires new settings into the manager and notifier | 4, 7 |

---

## PHASE 1 — Harden the driver

*Ships independently. Worth merging even if Phases 2–4 are abandoned: the Game_shelf Repair
button already hits this unbounded path today.*

---

### Task 1: Driver timeout + process-group kill

**Files:**
- Modify: `src/orchestrator/platform/steam/prefill_driver.py`
- Modify: `src/orchestrator/core/settings.py` (add one setting near line 92, by `steam_prefill_binary`)
- Modify: `src/orchestrator/agent/app.py:63-70` (pass the setting through)
- Test: `tests/platform/steam/test_prefill_driver.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `SteamPrefillDriver.__init__(..., timeout_sec: float = 36000.0)`;
  `PrefillResult(ok=False, raw="timeout: ...")` on expiry. Task 2 and Task 3 rely on both.

**Why:** `prefill_apps` currently awaits `communicate()` with no timeout. On 2026-08-12 a
SteamPrefill run was alive 5 days 18 hours after logging `Prefill complete!`. The host cron caps
this with `timeout -k 60 10h`; the orchestrator path has no equivalent, so it is strictly less
safe than the cron it will replace.

- [ ] **Step 1: Write the failing tests**

Add to `tests/platform/steam/test_prefill_driver.py`:

```python
def _hanging_binary(tmp_path):
    """Mimics the 2026-08-12 incident: prints success, then never exits."""
    p = tmp_path / "HangingSteamPrefill"
    p.write_text("#!/bin/sh\necho 'Prefill complete!'\nsleep 300\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _group_spawning_binary(tmp_path, marker):
    """Spawns a background child that outlives its parent unless the whole
    process GROUP is killed. The child writes `marker` after a delay."""
    p = tmp_path / "GroupSteamPrefill"
    p.write_text(f"#!/bin/sh\n(sleep 5; echo pwned > {marker}) &\nsleep 300\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.mark.asyncio
async def test_prefill_apps_times_out_instead_of_hanging(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    d = SteamPrefillDriver(
        binary=_hanging_binary(tmp_path), config_dir=cfg, timeout_sec=1.0
    )
    res = await d.prefill_apps([730])
    assert res.ok is False
    assert "timeout" in res.raw.lower()


@pytest.mark.asyncio
async def test_prefill_apps_restores_selection_even_on_timeout(tmp_path):
    """The operator's selection must survive the timeout path, or a timed-out
    run leaves the orchestrator's temporary app list as the cron's input."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "selectedAppsToPrefill.json").write_text("[111, 222]")
    d = SteamPrefillDriver(
        binary=_hanging_binary(tmp_path), config_dir=cfg, timeout_sec=1.0
    )
    await d.prefill_apps([730])
    assert json.loads((cfg / "selectedAppsToPrefill.json").read_text()) == [111, 222]


@pytest.mark.asyncio
async def test_timeout_kills_the_whole_process_group(tmp_path):
    """Killing only the direct child leaves grandchildren alive — exactly the
    cron's known weakness, where `timeout` kills the docker exec client while
    the in-container SteamPrefill runs on."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    marker = tmp_path / "child-survived.txt"
    d = SteamPrefillDriver(
        binary=_group_spawning_binary(tmp_path, marker), config_dir=cfg, timeout_sec=1.0
    )
    await d.prefill_apps([730])
    await asyncio.sleep(7)  # past the child's 5s delay
    assert not marker.exists(), "background child survived the group kill"
```

Add `import asyncio` to the test file's imports if absent.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `.venv/bin/python -m pytest tests/platform/steam/test_prefill_driver.py -q -k "timeout or group"`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'timeout_sec'`.

- [ ] **Step 3: Add the setting**

In `src/orchestrator/core/settings.py`, immediately after `steam_prefill_config_dir` (~line 93):

```python
    # Hard cap on a single SteamPrefill run. Matches the host cron's RUN_MAX=10h.
    # On 2026-08-12 a --force run was alive 5d18h having already logged
    # "Prefill complete!" and never exited; without this the agent job would
    # wait forever and the next tick would start a second concurrent run.
    steam_prefill_timeout_sec: float = Field(default=36000.0, gt=0)
```

`Field` is already imported in this module.

- [ ] **Step 4: Implement the timeout + group kill**

In `prefill_driver.py`, add `import contextlib` and `import signal` to the imports.

Change `__init__` to accept and store the timeout:

```python
    def __init__(
        self,
        *,
        binary: Path,
        config_dir: Path,
        home: Path | None = None,
        timeout_sec: float = 36000.0,
    ) -> None:
```

and add, after `self._home = ...`:

```python
        self._timeout_sec = float(timeout_sec)
```

Add this method to the class:

```python
    async def _kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the process GROUP, escalating to SIGKILL after 60s.

        The group, not the process: SteamPrefill may have spawned children, and
        killing only the direct child is the host cron's known weakness — its
        `timeout` kills the docker exec client while the in-container process
        runs on (its own alert text admits this).
        """
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
```

In `prefill_apps`, add `start_new_session=True` to the `create_subprocess_exec` call (this is
what puts the child in its own group so `killpg` cannot signal the orchestrator itself), and
replace the bare `out, _ = await proc.communicate()` with:

```python
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout_sec
                )
            except TimeoutError:
                await self._kill_process_group(proc)
                return PrefillResult(
                    ok=False,
                    raw=f"timeout: SteamPrefill exceeded {self._timeout_sec:.0f}s and was killed",
                )
```

Leave the existing outer `finally:` block that restores the prior selection exactly as it is —
it already covers the timeout path.

- [ ] **Step 5: Wire the setting through the agent**

In `src/orchestrator/agent/app.py`, inside the `SteamPrefillDriver(...)` construction (~line 63),
add as the last argument:

```python
                timeout_sec=settings.steam_prefill_timeout_sec,
```

- [ ] **Step 6: Run the tests and verify they pass**

Run: `.venv/bin/python -m pytest tests/platform/steam/test_prefill_driver.py -q`
Expected: PASS (all, including the pre-existing driver tests).

- [ ] **Step 7: Full verification**

```bash
.venv/bin/python -m ruff check src/ tests/
.venv/bin/python -m ruff format src/ tests/
.venv/bin/python -m mypy src/
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```
Expected: all clean; suite passes (baseline 1641 passed, 3 deselected — your count will be higher).

- [ ] **Step 8: Get approval, mark, and commit**

Present the diff summary to the human and get explicit approval. Then:

```bash
.claude/framework/hooks/mark-evaluated.sh "add SteamPrefill run timeout and process group kill"
```
(own command, repo root, relative path, no special characters)

```bash
git add src/orchestrator/platform/steam/prefill_driver.py src/orchestrator/core/settings.py src/orchestrator/agent/app.py tests/platform/steam/test_prefill_driver.py
git commit -m "fix(steam): cap SteamPrefill runs with a timeout and process-group kill"
```

---

### Task 2: `--recently-purchased` driver mode

**Files:**
- Modify: `src/orchestrator/platform/steam/prefill_driver.py`
- Test: `tests/platform/steam/test_prefill_driver.py`

**Interfaces:**
- Consumes: Task 1's `self._timeout_sec` and `_kill_process_group`.
- Produces: `SteamPrefillDriver.prefill_recent() -> PrefillResult`. Task 3 exposes it over HTTP;
  Task 7 schedules it.

**Why:** Decided in spec OQ1. `--recently-purchased` is not running anywhere since the v4 cron
rewrite (2026-08-12), so new-purchase discovery is currently incidental rather than deliberate.
This mode does **not** write `selectedAppsToPrefill.json` — SteamPrefill derives the set itself —
so it skips the save/restore dance entirely.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_prefill_recent_passes_the_flag(tmp_path):
    cfg = tmp_path / "Config"
    cfg.mkdir()
    argv = tmp_path / "argv.txt"
    p = tmp_path / "ArgvSteamPrefill"
    p.write_text(f'#!/bin/sh\necho "$@" > {argv}\nexit 0\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    d = SteamPrefillDriver(binary=p, config_dir=cfg)
    res = await d.prefill_recent()
    assert res.ok is True
    recorded = argv.read_text()
    assert "--recently-purchased" in recorded
    assert "--no-ansi" in recorded
    assert "--force" not in recorded  # scheduled prefill is never force (spec OQ4)


@pytest.mark.asyncio
async def test_prefill_recent_does_not_touch_the_selection_file(tmp_path):
    """SteamPrefill derives the recent set itself, so the operator's selection
    must be left completely alone — not rewritten and restored."""
    cfg = tmp_path / "Config"
    cfg.mkdir()
    sel = cfg / "selectedAppsToPrefill.json"
    sel.write_text("[111, 222]")
    before = sel.stat().st_mtime_ns
    d = SteamPrefillDriver(binary=_fake_binary(tmp_path), config_dir=cfg)
    await d.prefill_recent()
    assert json.loads(sel.read_text()) == [111, 222]
    assert sel.stat().st_mtime_ns == before, "selection file was rewritten"
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/platform/steam/test_prefill_driver.py -q -k recent`
Expected: FAIL — `AttributeError: 'SteamPrefillDriver' object has no attribute 'prefill_recent'`.

- [ ] **Step 3: Refactor the run path, then add the mode**

In `prefill_driver.py`, extract the subprocess mechanics from `prefill_apps` into a private
helper so both modes share the timeout and kill logic. Add:

```python
    async def _run(self, *extra_args: str) -> PrefillResult:
        """Run the SteamPrefill binary with the shared timeout + kill semantics."""
        args = [str(self._binary), "prefill", "--no-ansi", *extra_args]
        env = None if self._home is None else {**os.environ, "HOME": str(self._home)}
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self._config_dir.parent),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_sec)
        except TimeoutError:
            await self._kill_process_group(proc)
            return PrefillResult(
                ok=False,
                raw=f"timeout: SteamPrefill exceeded {self._timeout_sec:.0f}s and was killed",
            )
        raw = out.decode("utf-8", "replace")
        return PrefillResult(ok=(proc.returncode == 0), raw=raw[-4000:])
```

Rewrite `prefill_apps` to use it, keeping the selection save/restore:

```python
    async def prefill_apps(self, app_ids: list[int], *, force: bool = False) -> PrefillResult:
        """Write our app selection, run SteamPrefill, then restore the operator's
        prior selection. Returns ok=True iff exit code 0."""
        prior = self._selection_path.read_text() if self._selection_path.exists() else None
        self._selection_path.write_text(json.dumps([int(a) for a in app_ids]))
        try:
            return await self._run(*(["--force"] if force else []))
        finally:
            if prior is not None:
                self._selection_path.write_text(prior)
```

Add the new mode:

```python
    async def prefill_recent(self) -> PrefillResult:
        """Prefill newly-purchased apps using SteamPrefill's own discovery.

        Deliberately does NOT touch selectedAppsToPrefill.json: SteamPrefill
        derives the recent set itself, so there is no selection to save or
        restore, and no window in which a concurrent reader would see our
        temporary list. Never passes --force (spec OQ4: force is operator-only).
        """
        return await self._run("--recently-purchased")
```

Keep the existing comment about `cwd` (SteamPrefill resolves `./Config` relative to the working
directory) — move it into `_run` so it is not lost.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m pytest tests/platform/steam/test_prefill_driver.py -q`
Expected: PASS, including all pre-existing driver tests (the refactor must not change them).

- [ ] **Step 5: Full verification**

```bash
.venv/bin/python -m ruff check src/ tests/ && .venv/bin/python -m ruff format src/ tests/ && .venv/bin/python -m mypy src/
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

- [ ] **Step 6: Get approval, mark, and commit**

```bash
.claude/framework/hooks/mark-evaluated.sh "add recently purchased prefill mode to the Steam driver"
git add src/orchestrator/platform/steam/prefill_driver.py tests/platform/steam/test_prefill_driver.py
git commit -m "feat(steam): add --recently-purchased driver mode"
```

---

### Task 3: Agent single-flight guard + recent endpoint

**Files:**
- Modify: `src/orchestrator/agent/routers/steam.py`
- Test: `tests/agent/test_steam_router.py` (create if absent — check `ls tests/agent/` first)

**Interfaces:**
- Consumes: Task 2's `prefill_recent()`.
- Produces: `POST /v1/steam/prefill` returns the in-flight `job_id` when one is running;
  `POST /v1/steam/prefill-recent` returns `{"job_id": ...}`.

**Why:** `start_prefill` spawns its runner with no mutual exclusion. Two concurrent SteamPrefill
invocations share one auth/cache and corrupt state (spec ① §6). Today only the cron's `flock`
prevents this — which disappears in Phase 4.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_second_prefill_returns_the_in_flight_job_id(agent_client):
    """Two concurrent SteamPrefill processes corrupt the shared auth/cache, so
    the second request must dedup onto the running job, not start a rival."""
    first = await agent_client.post("/v1/steam/prefill", json={"app_ids": [730]})
    second = await agent_client.post("/v1/steam/prefill", json={"app_ids": [440]})
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]


@pytest.mark.asyncio
async def test_prefill_recent_endpoint_starts_a_job(agent_client):
    r = await agent_client.post("/v1/steam/prefill-recent")
    assert r.status_code == 202
    assert "job_id" in r.json()
```

Use whatever agent-app fixture already exists in `tests/agent/`. If there is none, build the app
via `orchestrator.agent.app.create_app()` and stub `app.state.prefill_driver` with a fake whose
`prefill_apps`/`prefill_recent` sleep briefly and return `PrefillResult(ok=True, raw="")`.

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/agent/test_steam_router.py -q -k "in_flight or recent"`
Expected: FAIL — second job_id differs; `/v1/steam/prefill-recent` returns 404.

- [ ] **Step 3: Implement the guard**

In `src/orchestrator/agent/routers/steam.py`, add a module-level helper:

```python
def _prefill_gate(request: Request) -> dict[str, Any]:
    """Single-flight state for SteamPrefill, held on app.state.

    SteamPrefill is not built for concurrent invocations sharing one auth/cache
    (see the re-arch spec). Until Phase 4 the host cron's flock provides this;
    afterwards the agent is the only thing standing between two scheduler ticks
    and a corrupted Config/.
    """
    state = request.app.state
    if not hasattr(state, "steam_prefill_gate"):
        state.steam_prefill_gate = {"lock": asyncio.Lock(), "job_id": None}
    gate: dict[str, Any] = state.steam_prefill_gate
    return gate
```

In `start_prefill`, immediately after `_validate_app_ids(body.app_ids)`:

```python
    gate = _prefill_gate(request)
    if gate["job_id"] is not None:
        store = request.app.state.agent_jobs
        running = store.get(gate["job_id"])
        if running is not None and running.get("state") not in ("done", "failed"):
            _log.info("steam_prefill.dedup_hit", job_id=gate["job_id"])
            return {"job_id": gate["job_id"]}
```

Set `gate["job_id"] = job_id` right after `job_id = store.create()`, and clear it in the
runner's `finally` (`gate["job_id"] = None`).

**Check the exact key name** the job store uses for state by reading
`src/orchestrator/agent/jobs.py::set_done`/`set_failed` — use whatever those write, not a guess.

Add the new endpoint, mirroring `start_prefill`'s body-task pattern (including the
`sync_manifests_to_archive` capture on success — a recent-purchase run writes manifests too, and
skipping the capture reintroduces the false-Partial bug):

```python
@router.post("/v1/steam/prefill-recent", status_code=status.HTTP_202_ACCEPTED)
async def start_prefill_recent(request: Request) -> dict[str, str]:
    ...same gate check, same job/bg-task shape, calling driver.prefill_recent()...
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m pytest tests/agent/ -q`

- [ ] **Step 5: Full verification, then approval, mark, commit**

```bash
.venv/bin/python -m ruff check src/ tests/ && .venv/bin/python -m ruff format src/ tests/ && .venv/bin/python -m mypy src/
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
.claude/framework/hooks/mark-evaluated.sh "add single flight guard and recent prefill endpoint to the agent"
git add src/orchestrator/agent/routers/steam.py tests/agent/
git commit -m "feat(agent): single-flight SteamPrefill + prefill-recent endpoint"
```

- [ ] **Step 6: STOP — Phase 1 gate**

Phase 1 is independently shippable. Update `CHANGELOG.md` (a `### Fixed` entry under
`## [Unreleased]`, dated, explaining the 5d18h incident and the timeout/group-kill/single-flight
fix), commit, open a PR, and **hand back to the human** before starting Phase 2.

---

## PHASE 2 — Alerting and stall detection

*Requires the human to have provisioned a Gmail app password. Confirm before starting.*

---

### Task 4: The notifier

**Files:**
- Create: `src/orchestrator/core/notify.py`
- Modify: `src/orchestrator/core/settings.py`
- Test: `tests/core/test_notify.py` (new)

**Interfaces:**
- Produces: `Notifier(settings).send(key: str, subject: str, body: str) -> bool`
  Returns `True` if sent, `False` if suppressed or failed. **Never raises.** Task 6 calls it.

**Why:** Spec OQ2 Option A. The orchestrator has no notification capability today; cache-catcher's
SMTP is unreachable from the LXC (verified: no docker socket in the agent, no published ports).

- [ ] **Step 1: Add the settings**

In `settings.py`, near the other feature flags:

```python
    # First outbound-notification capability (spec OQ2 Option A). Default OFF so
    # CI, dev, and any un-provisioned deploy never attempt SMTP.
    alerts_enabled: bool = False
    alert_smtp_host: str = "smtp.gmail.com"
    alert_smtp_port: int = Field(default=587, gt=0, le=65535)
    alert_smtp_username: str = ""
    alert_smtp_password: SecretStr = SecretStr("")
    alert_to: str = ""
    alert_from: str = ""
    # Per-condition cooldown: a persistent stall must not email every tick, or
    # the alert becomes noise and gets filtered. Mirrors the host cron resetting
    # its skip counter after alerting.
    alert_cooldown_sec: float = Field(default=21600.0, gt=0)
```

Add a model validator that fails fast when enabled but unconfigured — silently-disabled alerting
is the exact failure this phase removes:

```python
    @model_validator(mode="after")
    def _check_alert_config(self) -> Settings:
        if self.alerts_enabled:
            missing = [
                name
                for name, value in (
                    ("alert_smtp_username", self.alert_smtp_username),
                    ("alert_smtp_password", self.alert_smtp_password.get_secret_value()),
                    ("alert_to", self.alert_to),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"alerts_enabled=True but not configured: {', '.join(missing)}"
                )
        return self
```

Import `model_validator` from pydantic if not already imported. **Check whether the file already
has a `@model_validator(mode="after")`** — if so, add these checks to it rather than defining a
second one with a clashing method name.

- [ ] **Step 2: Write the failing tests**

`tests/core/test_notify.py`:

```python
"""Tests for the orchestrator's SMTP notifier (spec OQ2 Option A)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from orchestrator.core.settings import Settings

VALID_TOKEN = "a" * 32


def _settings(**kw):
    base = dict(
        orchestrator_token=VALID_TOKEN,
        alerts_enabled=True,
        alert_smtp_username="bot@example.com",
        alert_smtp_password=SecretStr("app-password"),
        alert_to="karl@example.com",
    )
    base.update(kw)
    return Settings(**base)


class _FakeSMTP:
    """Records what would have been sent. Never touches the network."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def __call__(self, message, settings):
        if self.fail:
            raise OSError("connection refused")
        self.sent.append(message)


def test_alerts_enabled_without_credentials_fails_fast():
    with pytest.raises(ValidationError):
        Settings(orchestrator_token=VALID_TOKEN, alerts_enabled=True)


def test_send_delivers_when_enabled():
    from orchestrator.core.notify import Notifier

    fake = _FakeSMTP()
    n = Notifier(_settings(), transport=fake)
    assert n.send("k", "subject", "body") is True
    assert len(fake.sent) == 1


def test_disabled_notifier_does_not_send():
    from orchestrator.core.notify import Notifier

    fake = _FakeSMTP()
    n = Notifier(Settings(orchestrator_token=VALID_TOKEN), transport=fake)
    assert n.send("k", "s", "b") is False
    assert fake.sent == []


def test_same_key_is_suppressed_within_the_cooldown():
    from orchestrator.core.notify import Notifier

    fake = _FakeSMTP()
    n = Notifier(_settings(alert_cooldown_sec=3600.0), transport=fake)
    assert n.send("stall", "s", "b") is True
    assert n.send("stall", "s", "b") is False
    assert len(fake.sent) == 1


def test_different_keys_are_not_suppressed():
    from orchestrator.core.notify import Notifier

    fake = _FakeSMTP()
    n = Notifier(_settings(), transport=fake)
    assert n.send("stall", "s", "b") is True
    assert n.send("timeout", "s", "b") is True


def test_send_failure_never_raises():
    """An alert failure must never fail the job that triggered it — the host
    cron does the same with `|| say ALERT-SEND-FAILED`."""
    from orchestrator.core.notify import Notifier

    fake = _FakeSMTP()
    fake.fail = True
    n = Notifier(_settings(), transport=fake)
    assert n.send("k", "s", "b") is False


def test_password_is_redacted_in_logs():
    """core/logging.py::_SENSITIVE_KEY_RE already matches 'password'. Assert it
    rather than assuming it."""
    from orchestrator.core.logging import _redact_sensitive_values

    out = _redact_sensitive_values({"alert_smtp_password": "hunter2"})
    assert "hunter2" not in str(out)
```

**Before writing this test, open `src/orchestrator/core/logging.py` and confirm the exact name and
signature of the redaction function** (~line 194). Adjust the last test to match; do not invent an
API.

- [ ] **Step 3: Run and watch them fail**

Run: `.venv/bin/python -m pytest tests/core/test_notify.py -q`
Expected: FAIL — `ModuleNotFoundError: orchestrator.core.notify`.

- [ ] **Step 4: Implement the notifier**

Create `src/orchestrator/core/notify.py`:

```python
"""Outbound alert notifier (spec OQ2 Option A).

The orchestrator's first outbound-notification capability. Chosen over reusing
the NAS cache-catcher's SMTP because that path is unreachable from the LXC: the
agent has no docker socket and cache-catcher publishes no ports.

Two invariants:
- send() NEVER raises. An alert failure must not fail the job that triggered it.
- The same key does not re-send within the cooldown, so a persistent stall does
  not email every tick and get filtered as noise.
"""

from __future__ import annotations

import smtplib
import time
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Callable

import structlog

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)


def _smtp_send(message: EmailMessage, settings: Settings) -> None:
    with smtplib.SMTP(settings.alert_smtp_host, settings.alert_smtp_port, timeout=30) as s:
        s.starttls()
        s.login(settings.alert_smtp_username, settings.alert_smtp_password.get_secret_value())
        s.send_message(message)


class Notifier:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: Callable[[EmailMessage, Any], None] = _smtp_send,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._last_sent: dict[str, float] = {}

    def send(self, key: str, subject: str, body: str) -> bool:
        """Return True if an email was sent; False if disabled, suppressed, or failed."""
        s = self._settings
        if not s.alerts_enabled:
            return False
        now = time.monotonic()
        previous = self._last_sent.get(key)
        if previous is not None and (now - previous) < s.alert_cooldown_sec:
            _log.info("alert.suppressed_by_cooldown", alert_key=key)
            return False
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = s.alert_from or s.alert_smtp_username
        message["To"] = s.alert_to
        message.set_content(body)
        try:
            self._transport(message, s)
        except Exception as e:
            _log.error("alert.send_failed", alert_key=key, reason=str(e)[:200])
            return False
        self._last_sent[key] = now
        _log.info("alert.sent", alert_key=key)
        return True
```

- [ ] **Step 5: Run, verify pass, then full verification**

```bash
.venv/bin/python -m pytest tests/core/test_notify.py -q
.venv/bin/python -m ruff check src/ tests/ && .venv/bin/python -m ruff format src/ tests/ && .venv/bin/python -m mypy src/
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

- [ ] **Step 6: Approval, mark, commit**

```bash
.claude/framework/hooks/mark-evaluated.sh "add the orchestrator SMTP notifier with per key cooldown"
git add src/orchestrator/core/notify.py src/orchestrator/core/settings.py tests/core/test_notify.py
git commit -m "feat(core): add SMTP notifier with per-condition cooldown"
```

---

### Task 5: Wire the notifier into app state

**Files:**
- Modify: `src/orchestrator/api/main.py`
- Test: `tests/api/test_app_factory.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_app_exposes_a_notifier(unit_app):
    assert hasattr(unit_app.state, "notifier")
```

- [ ] **Step 2: Run, watch it fail**

Run: `.venv/bin/python -m pytest tests/api/test_app_factory.py -q -k notifier`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement**

In `create_app()` in `main.py`, near where other `app.state` attributes are set:

```python
    app.state.notifier = Notifier(settings)
```

with `from orchestrator.core.notify import Notifier` at the top.

- [ ] **Step 4: Verify, approve, mark, commit**

```bash
.venv/bin/python -m pytest tests/api/ -q
.claude/framework/hooks/mark-evaluated.sh "expose the notifier on app state"
git add src/orchestrator/api/main.py tests/api/test_app_factory.py
git commit -m "feat(api): expose the notifier on app state"
```

---

### Task 6: Stall detection

**Files:**
- Modify: `src/orchestrator/scheduler/jobs.py`
- Modify: `src/orchestrator/scheduler/manager.py`
- Test: `tests/scheduler/test_jobs.py`

**Interfaces:**
- Consumes: Task 4's `Notifier.send`.
- Produces: `async def check_stalled_prefill(pool: Pool, notifier: Notifier, timeout_sec: float) -> int`
  — returns the number of stalled jobs found. Registered on a cron by the manager.

**Why:** Replaces `run-steam-prefill.sh`'s skip counter. A job row with `started_at` set and no
`finished_at` past the timeout is directly queryable — strictly more observable than a counter in
a file.

- [ ] **Step 1: Write the failing test**

```python
class TestStalledPrefillCheck:
    async def test_alerts_on_a_job_running_past_the_timeout(self, pool):
        from orchestrator.scheduler.jobs import check_stalled_prefill

        async with pool.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO jobs (kind, platform, state, source, started_at) "
                "VALUES ('prefill', 'steam', 'running', 'scheduler', "
                "datetime('now','-20 hours'))"
            )
        sent = []

        class _N:
            def send(self, key, subject, body):
                sent.append(key)
                return True

        found = await check_stalled_prefill(pool, _N(), timeout_sec=36000.0)
        assert found == 1
        assert sent == ["steam_prefill_stall"]

    async def test_no_alert_when_within_the_timeout(self, pool):
        from orchestrator.scheduler.jobs import check_stalled_prefill

        async with pool.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO jobs (kind, platform, state, source, started_at) "
                "VALUES ('prefill', 'steam', 'running', 'scheduler', "
                "datetime('now','-1 hours'))"
            )
        sent = []

        class _N:
            def send(self, key, subject, body):
                sent.append(key)
                return True

        assert await check_stalled_prefill(pool, _N(), timeout_sec=36000.0) == 0
        assert sent == []
```

- [ ] **Step 2: Run, watch it fail** — `ImportError: cannot import name 'check_stalled_prefill'`.

- [ ] **Step 3: Implement**

In `scheduler/jobs.py`:

```python
async def check_stalled_prefill(pool: Pool, notifier: Any, timeout_sec: float) -> int:
    """Alert on prefill jobs still 'running' past the timeout.

    Replaces the host cron's consecutive-skip counter. The 2026-08-12 incident
    (a run alive 5d18h after logging 'Prefill complete!') was invisible for six
    days precisely because nothing checked this. Never raises — a failing
    scheduler tick must not degrade APScheduler.
    """
    try:
        rows = await pool.read_all(
            "SELECT id, platform, started_at FROM jobs "
            "WHERE kind='prefill' AND state='running' "
            "AND started_at <= datetime('now', ?)",
            (f"-{int(timeout_sec)} seconds",),
        )
    except PoolError as e:
        _log.error("scheduler.stall_check_failed", reason=str(e)[:200])
        return 0
    if not rows:
        return 0
    ids = ", ".join(str(r["id"]) for r in rows)
    notifier.send(
        "steam_prefill_stall",
        f"lancache ALERT: {len(rows)} prefill job(s) stalled",
        f"Prefill job(s) {ids} have been 'running' for longer than "
        f"{int(timeout_sec)}s.\n\n"
        f"A SteamPrefill process can finish its work and then never exit "
        f"(seen 2026-08-12, alive 5d18h). Check the agent:\n"
        f"  ps -eo pid,etime,args | grep SteamPrefill\n",
    )
    _log.warning("scheduler.prefill_stalled", count=len(rows))
    return len(rows)
```

Register it in `manager.py` alongside the other jobs, on a `CronTrigger` — hourly is ample
(`0 * * * *`). Pass the notifier in via the manager constructor, following exactly how
`agent_client` is already threaded through (`__init__` param → `self._x` → `args=(...)`).

- [ ] **Step 4: Verify, approve, mark, commit**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
.claude/framework/hooks/mark-evaluated.sh "add stalled prefill detection with alerting"
git add src/orchestrator/scheduler/ tests/scheduler/
git commit -m "feat(scheduler): alert on prefill jobs stalled past the timeout"
```

- [ ] **Step 5: STOP — Phase 2 gate.** Update CHANGELOG, PR, hand back to the human.

---

## PHASE 3 — Scheduled Steam prefill

---

### Task 7: The sibling Steam job

**Files:**
- Modify: `src/orchestrator/scheduler/jobs.py`
- Modify: `src/orchestrator/scheduler/manager.py`
- Modify: `src/orchestrator/core/settings.py`
- Modify: `src/orchestrator/api/main.py`
- Test: `tests/scheduler/test_jobs.py`, `tests/scheduler/test_manager.py`

**Interfaces:**
- Produces: `enqueue_scheduled_steam_prefill(pool) -> int`;
  settings `scheduled_steam_prefill_enabled: bool`, `scheduled_steam_prefill_cron: str`.

**Why:** Spec OQ3 — a **sibling**, not an extension of the Epic job.

**CRITICAL — Steam's job model is NOT per-game.** This was discovered while writing this plan and
is the single most important thing to get right:

- The **Epic** job enqueues *one prefill job per game*, because the orchestrator computes which
  Epic games need work (`status <> 'up_to_date'`).
- **SteamPrefill does its own version-diff** over its selection list. A single
  `SteamPrefill prefill` run reports e.g. `Updated 5 | Up To Date 1125` — it decides internally
  what is stale. The orchestrator must **not** compute a Steam candidate set.

Therefore the scheduled Steam job enqueues **exactly one job row per tick** that means "run
SteamPrefill over its existing selection", mirroring what the cron does. Two consequences:

1. Do **not** query `games` for candidates. Applied to Steam, Epic's predicate would enqueue all
   **1,363** `not_downloaded` rows — games deliberately outside the selection.
2. The selection file must **not** be rewritten. `prefill_apps(app_ids)` writes and restores it;
   this path must leave it untouched, so it needs a third driver mode.

**Routing precedent:** the `prefill` job already routes on a payload marker —
`jobs/handlers/prefill.py::_payload_force` reads `{"force": true}` written by the trigger. Use the
same mechanism with `{"mode": "selection"}`.

- [ ] **Step 1: Add the driver mode (mirrors Task 2)**

In `prefill_driver.py`:

```python
    async def prefill_selection(self) -> PrefillResult:
        """Prefill SteamPrefill's existing selection, letting it do its own
        version-diff (a run reports 'Updated N | Up To Date M').

        Does NOT write selectedAppsToPrefill.json — this is the scheduled
        whole-selection pass, so there is no per-app targeting and no selection
        to save or restore. Never passes --force (spec OQ4).
        """
        return await self._run()
```

Test it exactly like `test_prefill_recent_does_not_touch_the_selection_file`, asserting the
mtime is unchanged and that neither `--recently-purchased` nor `--force` is passed.

- [ ] **Step 2: Add the agent endpoint**

`POST /v1/steam/prefill-selection`, same single-flight gate and background-task shape as Task 3,
calling `driver.prefill_selection()`. Include the `sync_manifests_to_archive` capture on success.

Add `AgentClient.prefill_selection()` in `src/orchestrator/clients/agent_client.py`, following
the existing `prefilled_apps()` / prefill-job-polling methods.

- [ ] **Step 3: Write the failing scheduler tests**

```python
class TestScheduledSteamPrefill:
    async def test_enqueues_exactly_one_selection_job(self, pool):
        """SteamPrefill does its own version-diff over its selection, so a tick
        is ONE job meaning 'run the selection pass' — never one job per game."""
        from orchestrator.scheduler.jobs import enqueue_scheduled_steam_prefill

        assert await enqueue_scheduled_steam_prefill(pool) == 1
        rows = await pool.read_all(
            "SELECT payload FROM jobs WHERE kind='prefill' AND platform='steam'"
        )
        assert len(rows) == 1
        assert json.loads(rows[0]["payload"]) == {"mode": "selection"}

    async def test_does_not_enqueue_per_game_rows(self, pool):
        """Guards the footgun: Epic's predicate applied to Steam would enqueue
        all 1,363 not_downloaded rows, i.e. games deliberately excluded."""
        from orchestrator.scheduler.jobs import enqueue_scheduled_steam_prefill

        async with pool.write_transaction() as tx:
            for i in range(3):
                await tx.execute(
                    "INSERT INTO games (platform, app_id, title, owned, status) "
                    "VALUES ('steam', ?, ?, 1, 'not_downloaded')",
                    (str(900 + i), f"Not Selected {i}"),
                )
        await enqueue_scheduled_steam_prefill(pool)
        rows = await pool.read_all(
            "SELECT id FROM jobs WHERE kind='prefill' AND platform='steam'"
        )
        assert len(rows) == 1, "must be one selection job, not one per game"

    async def test_dedups_against_an_in_flight_selection_run(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_steam_prefill

        assert await enqueue_scheduled_steam_prefill(pool) == 1
        assert await enqueue_scheduled_steam_prefill(pool) == 0
```

- [ ] **Step 4: Run and watch them fail**, then implement:

```python
_SELECTION_PAYLOAD = '{"mode": "selection"}'


async def enqueue_scheduled_steam_prefill(pool: Pool) -> int:
    """Enqueue ONE 'run the SteamPrefill selection' job, if none is in flight.

    Unlike Epic this is not per-game: SteamPrefill owns the version-diff over
    its own selection list, so the orchestrator's job is to trigger a pass, not
    to decide which apps are stale. Querying `games` for candidates here would
    enqueue the ~1,363 not_downloaded rows that are deliberately outside the
    selection. Never raises — a failing tick must not degrade APScheduler.
    """
    try:
        existing = await pool.read_one(
            "SELECT id FROM jobs WHERE kind='prefill' AND platform='steam' "
            "AND state IN ('queued','running') LIMIT 1"
        )
        if existing is not None:
            _log.info("scheduler.steam_prefill.dedup_hit", existing_job_id=existing["id"])
            return 0
        inserted = await pool.execute_write(
            "INSERT INTO jobs (kind, platform, state, source, payload) "
            "VALUES ('prefill', 'steam', 'queued', 'scheduler', ?)",
            (_SELECTION_PAYLOAD,),
        )
        _log.info("scheduler.steam_prefill.enqueued", count=inserted)
        return int(inserted)
    except PoolError as e:
        _log.error("scheduler.steam_prefill.failed", reason=str(e)[:200])
        return 0
```

- [ ] **Step 5: Route the handler**

In `jobs/handlers/prefill.py`, add a `_payload_mode(job) -> str` helper mirroring
`_payload_force` (same robustness: missing/NULL/non-JSON payload → default). When the mode is
`"selection"`, call the agent's `prefill_selection()` and skip the per-game path entirely — note
this job row has **no `game_id`**, so any code assuming one must be bypassed. Add a test that a
selection-mode job with `game_id IS NULL` completes without raising.

- [ ] **Step 6: Full verification.**

Settings to add:

```python
    scheduled_steam_prefill_enabled: bool = False  # opt-in until Phase 4
    # 1h15m after the host Steam cron (00/06/12/18 UTC), clear of the validation
    # sweep (0 3,9,15,21) and the Epic prefill (45 3,9,15,21).
    scheduled_steam_prefill_cron: str = "15 1,7,13,19 * * *"
```

Add a fail-fast cron validator copying `_validate_scheduled_prefill_cron` exactly.

Register in `manager.py` with `CronTrigger.from_crontab(..., timezone="UTC")`, mirroring
`SCHEDULED_PREFILL_JOB_ID`. Add `STEAM_PREFILL_JOB_ID = "scheduled_steam_prefill"`.

Add manager tests mirroring `TestScheduledPrefillRegistration`: registers when enabled, absent
when disabled, uses a `CronTrigger` not an `IntervalTrigger`, and the default slot is
minute `15`, hours `1,7,13,19`.

- [ ] **Step 6: Approval, mark, commit**

```bash
.claude/framework/hooks/mark-evaluated.sh "add the sibling scheduled Steam prefill job"
git commit -m "feat(scheduler): sibling scheduled Steam prefill job"
```

---

### Task 8: Daily recent-purchase schedule

**Files:** `src/orchestrator/scheduler/jobs.py`, `manager.py`, `settings.py`, tests.

**Why:** Spec OQ1 — a recent-purchase pass needs only daily cadence, while the selection pass
stays 6-hourly.

Same job model as Task 7 — one job row per tick, routed by payload — with `{"mode": "recent"}`
instead of `{"mode": "selection"}`.

- [ ] **Step 1: Write the failing tests**

```python
class TestScheduledSteamRecent:
    async def test_enqueues_one_recent_mode_job(self, pool):
        from orchestrator.scheduler.jobs import enqueue_scheduled_steam_recent

        assert await enqueue_scheduled_steam_recent(pool) == 1
        rows = await pool.read_all(
            "SELECT payload FROM jobs WHERE kind='prefill' AND platform='steam'"
        )
        assert json.loads(rows[0]["payload"]) == {"mode": "recent"}

    async def test_dedups_against_any_in_flight_steam_prefill(self, pool):
        """A recent pass and a selection pass are both SteamPrefill invocations
        and must not overlap — they share one auth/cache."""
        from orchestrator.scheduler.jobs import (
            enqueue_scheduled_steam_prefill,
            enqueue_scheduled_steam_recent,
        )

        assert await enqueue_scheduled_steam_prefill(pool) == 1
        assert await enqueue_scheduled_steam_recent(pool) == 0
```

- [ ] **Step 2: Run and watch them fail**, then implement `enqueue_scheduled_steam_recent` as a
  near-copy of `enqueue_scheduled_steam_prefill` with `_RECENT_PAYLOAD = '{"mode": "recent"}'`.
  **Keep the same dedup query** (any in-flight steam prefill blocks either mode) — the second
  test above is what proves it.

- [ ] **Step 3:** Extend `_payload_mode` routing in `jobs/handlers/prefill.py` so `"recent"`
  calls the agent's `prefill_recent()` (Task 3's endpoint).

- [ ] **Step 4:** Register in `manager.py` on `scheduled_steam_recent_cron`, default
  `"15 1 * * *"` (daily; reuses the first selection slot rather than adding a fifth window).
  Add the fail-fast cron validator, and manager tests mirroring Task 7's.

- [ ] **Step 5:** Full verification.

- [ ] **Step 6:** Approval, mark, commit, **STOP — Phase 3 gate.** CHANGELOG, PR, hand back.

**Deploy note for the human:** Phase 3 must run with `scheduled_steam_prefill_enabled=false` in
production initially. Enable it only when ready to observe, with the host cron still running —
double-prefill is wasteful but harmless, whereas an unobserved switchover is not.

---

## PHASE 4 — Retire the host cron

**This phase is operational, not code. Do NOT execute it as an agent — hand it to the human.**

### Go/no-go gate — all five must hold for 7 consecutive days

1. Every scheduled Steam prefill produced a terminal `jobs` row (no orphans).
2. At least one deliberate overlap was deduped, not double-run.
3. A deliberately induced stall raised an alert email.
4. `games.last_prefilled_at` for Steam tracks actual runs.
5. Steam `up_to_date` count stable or better (baseline 2026-08-14: **1,138**).

### Retirement steps (human, on the NAS)

1. Back up: `cp /var/spool/cron/crontabs/karl /home/karl/spool-karl.bak-<date>`
2. Remove the `0 */6 * * * run-steam-prefill.sh` line from **both**
   `/home/karl/lancache-host/prefill-cron-v4` (the master) **and**
   `/var/spool/cron/crontabs/karl` (the live spool).
   **`crontab <file>` does NOT work on this NAS** — `/var/spool/cron/crontabs` is root-owned so
   `mkstemp` fails. Write the spool file directly; it is karl-owned, mode 600. Cron reloads on
   mtime.
3. Keep `run-steam-prefill.sh` on disk for one release cycle as the rollback path.
4. Verify: no Steam prefill starts at the old slots; orchestrator job rows appear at the new ones.

### Rollback

Re-add the cron line and set `scheduled_steam_prefill_enabled=false`. Configuration only — no data
migration, nothing to undo in the database.
