# UAT-6 — Exploratory / Adversarial Agent Findings

## TOP-OF-FILE WARNING (read first)

Two high-impact correctness issues were found that the existing tests cannot
catch because the stubs in `test_library_sync_handler.py` skip the IPC layer
and the stubs in `test_auth_router.py` skip the subprocess layer:

- **F1 (SEV-2)** — A real Steam library response >64 KiB will crash the IPC
  read loop with an uncaught `ValueError`, fail all pending futures with
  `WorkerDiedError`, and trip the restart-storm guard. The 10 MiB
  `MAX_IPC_LINE_BYTES` cap in `protocol.py` is **dead code on the read
  path** — `asyncio.StreamReader.readline()` raises long before it.
- **F2 (SEV-3, leans SEV-2)** — Worker stderr is piped but never drained;
  a chatty steam-next + gevent run will deadlock the subprocess once the
  OS pipe buffer (typically 64 KiB on macOS, 1 MiB on Linux) fills.

Both are realistic on the first real-world Steam account with ≥ ~300 owned
apps; both are invisible to the current test suite.

## Methodology

Probed the BL11 surface for: IPC framing limits vs. real-world payload sizes,
subprocess pipe-deadlock conditions, schema-constraint violations the handler
doesn't pre-validate, race-window quantification on dedup, registry/state
leakage across tests, JSON encoding/log-leak hazards, and shutdown
correctness during a mid-handler run.

Read-only audit; no code modifications.

## Findings

### Finding 1 — Worker response >64 KiB crashes the IPC read loop (uncaught ValueError)
**Severity proposal:** SEV-2 (correctness; realistic data loss / availability
hit on first real library)
**Location:** `src/orchestrator/platform/steam/client.py:199`
**Reproduction:**
```python
# asyncio.StreamReader default buffer = 65536 bytes.
import asyncio
r = asyncio.StreamReader()
print(r._limit)  # 65536
help(asyncio.StreamReader.readline)
# "If limit is reached, ValueError will be raised."
```
The worker emits one JSON line per response. A library with ~600 apps × ~100
bytes/app (`{"app_id":N,"name":"…","depots":[…]}`) already exceeds 64 KiB.
The plan's own §F12.2 P11 mentions "100k+ apps" — that line is megabytes.

In `_read_loop` (client.py:198-203):
```python
while True:
    line = await self._process.stdout.readline()  # raises ValueError
    if not line:
        ...
```
The ValueError propagates out of `_read_loop`, the task ends with an
exception, and **no `_on_worker_died` is called from this path** — the
worker subprocess is still alive (it's only the orchestrator's reader that
died), so `WorkerDiedError` never fires for the pending future; the caller
gets `IPCTimeoutError` after 30s instead. The worker process leaks (no one
restarts the reader), and the restart-storm guard counter never increments,
so this can keep happening silently across requests until subprocess pid
exhaustion or operator restart.

`protocol.py:107-108`'s "10 MiB" cap is enforced inside `from_line()` — but
control never reaches `from_line()` because `readline()` raised first.

**Why it matters:** Real Steam accounts with even a moderate library size
will trigger this on the very first sync. The handler's nominal happy-path
is unverifiable until this is fixed.

**Fix sketch:** Pass `limit=MAX_IPC_LINE_BYTES` (or a smaller chosen value)
to `asyncio.create_subprocess_exec` — note `asyncio.subprocess` does NOT
take `limit=` directly; the workaround is `loop.subprocess_exec` with a
custom `StreamReader(limit=...)`, OR switch to chunked reads + a manual
buffer. Either way, catch `LimitOverrunError`/`ValueError` in `_read_loop`,
log, kill the subprocess, and call `_on_worker_died` so the guard works.

### Finding 2 — Worker stderr piped but never drained → pipe-buffer deadlock
**Severity proposal:** SEV-3 (leans SEV-2 if gevent or steam-next is verbose)
**Location:** `src/orchestrator/platform/steam/client.py:107`
**Reproduction:** `stderr=asyncio.subprocess.PIPE` with no reader. macOS
default pipe buffer = 64 KiB. Any sustained stderr output (steam-next
prints to stderr on connection wobble / cm-server changes / 2FA hints)
fills the buffer; subsequent worker stderr `write()` blocks; if it blocks
inside a gevent task, the gevent loop stalls; library.enumerate appears
to hang and times out as `IPCTimeoutError`.
**Why it matters:** Symptom looks like Steam being slow. Diagnosis path
runs through every other suspect first because nothing in our logs
records it.
**Fix sketch:** `stderr=asyncio.subprocess.DEVNULL` (simplest, but discards
diagnostic value) OR spawn a second drain task that logs each stderr line
as `steam_worker.stderr` events.

### Finding 3 — Auth auto-trigger races with manual POST and creates duplicate queued rows
**Severity proposal:** SEV-3 (functional dup, not a correctness bug given
UPSERT idempotency, but contradicts the dedup contract advertised in
`sync.py:7`)
**Location:** `src/orchestrator/api/routers/auth.py:143-166` and
`src/orchestrator/api/routers/sync.py:39-53`
**Reproduction:** Concurrent: (a) POST /auth completes 2FA → calls
`_queue_library_sync_job_best_effort` → SELECT returns None → INSERT;
(b) Operator simultaneously POSTs /library/sync → SELECT returns None
(if scheduled between (a)'s SELECT and INSERT). Both INSERTs commit;
two queued rows. The worker claims and runs each in turn.
**Why it matters:** The plan P8 acknowledges this race but only documents
the *concurrent-POST* case. The auth-success + manual-POST overlap path
isn't acknowledged, and tests don't cover it. UPSERT covers the data
correctness, but observability is degraded (two completed jobs for what
the operator did once) and the contract docstring in `sync.py:5-7`
overstates dedup robustness.
**Fix sketch:** Document the cross-flow race in the spec; OR add a
`UNIQUE(kind, platform) WHERE state IN ('queued','running')` partial
index in a new migration and treat the resulting `IntegrityError` as
the dedup signal.

### Finding 4 — `claim_next_job` cannot handle 1M+ queued jobs gracefully (no LIMIT on UPDATE rowscan)
**Severity proposal:** SEV-4 (speculative; only relevant under pathological
queue depth)
**Location:** `src/orchestrator/jobs/worker.py:50-57`
**Reproduction (speculative):** With sufficient queued backlog, the
`SELECT id ... LIMIT 1 + UPDATE WHERE id=? AND state='queued'` is fine.
But there is no defensive `LIMIT` on the secondary read at
worker.py:58-60. If a buggy concurrent path duplicates rows for that id
(can't happen with PRIMARY KEY, so this is moot), this would return more
than expected. Confirmed not exploitable; surfaced for completeness.

### Finding 5 — Handler logs raw `str(app)[:200]` on skipped apps → potential PII leak
**Severity proposal:** SEV-3 (privacy/observability)
**Location:** `src/orchestrator/jobs/handlers/library_sync.py:64-69`
**Reproduction:** A malformed library payload that includes a hypothetical
field with the user's email or session token (steam-next is closed-box;
we don't control what they put in `app`) would be persisted verbatim into
structlog at WARNING level. Not currently exploitable — `get_product_info`
doesn't return PII in published shapes — but the handler doesn't assume
that.
**Why it matters:** Logs flow to operator dashboards / external sinks. A
future steam-next version returning an `owner_account_id` or `purchase_id`
field for free→paid promotion tracking would leak. Defense in depth.
**Fix sketch:** Log only `list(app.keys())[:10]` and `type(title)` instead
of `str(app)[:200]`.

### Finding 6 — `library_enumerate` worker handler silently coerces non-string app names to `f"app_{app_id}"`
**Severity proposal:** SEV-4 (data quality, not security)
**Location:** `src/orchestrator/platform/steam/worker.py:228`
**Reproduction:** If steam-next ever returns `{"common": {"name": None}}`
or `{"common": {"name": 12345}}`, the worker constructs a synthetic name.
The handler then accepts it as a valid string (line 62 of library_sync.py
checks `isinstance(title, str)` — `f"app_{N}"` passes). Operator sees
"app_730" rows in the games table with no path to distinguish "Steam
returned junk" from "this game truly has no marketing name."
**Fix sketch:** Have the worker omit the app entirely when `common.name`
is missing/non-string; let the handler-side `skipped += 1` counter
surface it.

### Finding 7 — `_steam_client_singleton` module global leaks between tests / fastapi app instances
**Severity proposal:** SEV-4 (test hygiene; not a production issue)
**Location:** `src/orchestrator/api/routers/auth.py:94, 108-112`
**Reproduction:** Two `create_app()` calls in the same test session share
`_steam_client_singleton`. Test A calls `set_steam_client_singleton(real)`;
test B that doesn't reset DI overrides will see test A's client through
`get_steam_client_dep`. Currently masked because most tests use
`app.dependency_overrides`. Fragile.
**Fix sketch:** Move the singleton onto `app.state.steam_client`; have
`get_steam_client_dep` read it from `request.app.state`.

### Finding 8 — `_queue_library_sync_job_best_effort` swallows `Exception`-broad? No — narrow to PoolError. Confirmed safe; surfaced as a "not a finding."

(Moved to "Notes that AREN'T findings.")

## Test coverage gaps (no code defect, but tests missing)

- **64 KiB+ IPC response.** No test feeds a stdout line larger than
  StreamReader's default `_limit` through the real `_read_loop`. The unit
  tests in `test_library_sync_handler.py` use an in-Python stub that
  bypasses the subprocess entirely.
- **Worker stderr saturation.** No test fills the stderr pipe; no test
  asserts what happens when steam-next emits 100 KiB+ of warnings.
- **Concurrent auth-success + manual POST.** `test_auth_router.py` covers
  the dedup happy path within a single endpoint; the cross-endpoint race
  is untested.
- **Jobs worker mid-handler + lifespan shutdown.** Lifespan shutdown
  test (`tests/api/test_app_lifespan.py` if present, otherwise gap)
  doesn't assert that a job interrupted by `jobs_shutdown.set()` ends in
  state='running' (orphan) vs. state='failed' vs. 'queued'-replay. With
  the current code, a handler mid-execute_write that takes >5s after
  shutdown will be `task.cancel()`-ed (main.py:174), leaving the job
  row in state='running' forever. **No reaper on restart.**
- **Schema CHECK violation paths.** The worker can produce `app_id=""` if
  steam-next returns `{"app_id": 0}` (allowed — `int(0)` → "0", non-empty
  → fine). But if it returns `{"app_id": ""}`, `str("")` is "", which
  violates the `length(app_id) BETWEEN 1 AND 64` CHECK; pool raises
  IntegrityError; handler doesn't catch it; the *entire job* fails on
  one bad row. No test exercises this.
- **Unicode/control-char names.** No test for app names containing
  literal `\n`, `\0`, non-BMP chars, or RTL override (U+202E). `json.dumps`
  handles them, but the SQLite TEXT column accepts NUL bytes
  inconsistently (`length()` truncates at NUL on some builds).
- **Restart-storm guard interaction with library_enumerate.** No test
  for: worker dies mid-library.enumerate → claim_next_job picks up the
  same job again on next loop iteration (it's state='running' — yes, it
  won't be re-claimed; but it's also orphaned forever).

## Notes that AREN'T findings

- **JSON encoding** in `library_sync.py:71-73` uses `json.dumps(...,
  separators=(",", ":"))` with default `ensure_ascii=True` — backslashes,
  control chars, and non-BMP unicode are all safely escaped. No injection
  surface.
- **Credentials in logs.** Verified: `auth_begin` logs `username_present=
  True` not the value; worker.py never logs the password; `_send` writes
  to stdout only, not stderr; structlog config in `core/logging.py`
  doesn't include the request body. Auth path is clean.
- **`_challenges` dict memory leak.** `_sweep_expired_challenges` is called
  from `auth_begin`. If no one ever calls `auth_begin` again after an
  abandoned 2FA flow, the entry stays. Bounded by `CHALLENGE_TTL_SEC` +
  next-auth-attempt; given typical operator behavior, fine.
- **Best-effort dedup PoolError handling.** `_queue_library_sync_job_best_effort`
  catches only `PoolError`, not bare `Exception`. Verified — correct scope.
- **Jobs worker registry mutation across tests.** `tests/jobs/conftest.py`
  has an autouse snapshot/restore fixture for `HANDLERS`. Verified
  correct.
- **`mark_succeeded` after `mark_failed` race.** The state machine uses
  `WHERE state='running'` on both — once marked failed, mark_succeeded
  is a no-op. Verified safe.
- **CHECK constraint on jobs.kind.** Migration 001 includes 'library_sync'
  in the CHECK whitelist. Verified.

## Speculative items not promoted to findings
- 100k+ app library exhausting SQLite write throughput during the upsert
  loop — measurable but not tested; depends on hardware. Mentioned in
  plan as known limitation.
- `_client.licenses` returning a generator instead of a list (worker.py:
  188) — `for license_obj in licenses` works on either; `getattr(...) or
  []` short-circuits on truthy generators (a non-empty generator is
  truthy). Safe.
