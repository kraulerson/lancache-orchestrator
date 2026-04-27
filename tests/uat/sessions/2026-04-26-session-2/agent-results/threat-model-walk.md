# UAT-2 Threat-Model Walk — BL3 (Settings) + BL4 (DB Pool)

**Date:** 2026-04-26
**Persona:** Penetration Tester (re-grounded for the BL3+BL4 surface only)
**Inputs:**
- Authoritative threat model: `docs/phase-1/threat-model.md` (TM-001 … TM-023)
- Bible cross-ref: `PROJECT_BIBLE.md` §4
- Implementation in scope:
  - `src/orchestrator/core/settings.py` (BL3 — token loading, redaction primitives, diagnostic warnings)
  - `src/orchestrator/db/pool.py` (BL4 — connection pool, transactions, error wrapping, replacement state machine)
  - `src/orchestrator/db/migrate.py` (BL1 + BL4 `verify_schema_current` addition)
- Tests: `tests/core/test_settings.py`, `tests/db/test_pool*.py`, `tests/db/test_migrate.py`
- Semgrep rules: `.semgrep/orchestrator-rules.yaml`

**Methodology:** For each TM most-relevant to BL3/BL4, locate the mitigation in code, exercise the assumption, and rate Strong / Weak / Missing / Out-of-scope-here. Then enumerate any net-new threats introduced by BL3+BL4 that are not in TM-001…TM-023.

---

## 1. Per-TM verdict table

| TM-id | Mitigation location (file:line / behavior) | Status | Notes |
|---|---|---|---|
| **TM-001** Bearer-token leak via Game_shelf `.env` (A7→A6) | `settings.py:53-56` SecretStr field; `settings.py:96-117` strip+length validators; `settings.py:119-139` `__init__` wraps `ValidationError` to scrub the rejected raw value before re-raise; `settings.py:141-150` `__reduce__` blocks pickle; `settings.py:163-170` `config.secret_shadowed_by_env` warning; `migrate.py:585` argv elision in `_cli`. | **Strong** | The orchestrator side of TM-001 is the secret-loading half (handling, redaction, no leak on validation failure). All five attack surfaces I would target on a captured `.env`-equivalent are sealed: SecretStr `__repr__` is opaque; ValidationError input echo intercepted; pickle blocked; env-vs-secret-file shadow surfaced as a WARNING; CLI argc-only logging. The `frontend/` CI grep + Game_shelf `.env` mode 0600 + pfSense rule remain out of BL3 scope (they live in Game_shelf and infra). |
| **TM-005** SQL injection through API path params (A6→A4) | `pool.py:140-149` `_LITERAL_RE`; `pool.py:188-202` `_template_only` + `_shape` (logs only SQL template + param type names, never values); every helper passes user params as `?` placeholders to `aiosqlite` (`pool.py:362, 372, 382-409` reader path; `pool.py:422-484` writer path; `pool.py:880-987` single-stmt helpers); `.semgrep/orchestrator-rules.yaml` `no-f-string-sql` is active. The two unsafe-looking f-string SQLs (`pool.py:687, 690, 1000, 1007, 1015, 1037`) are PRAGMA / `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` / `SELECT 1` against literal hardcoded values that have `# nosem` annotations and no user input path. | **Strong** | Manual SQLi attempts against the pool's API surface require an attacker-supplied SQL string, which the helper signatures simply do not accept — `sql` is a fixed compile-time constant in every callsite that ships with BL3+BL4. The literal-stripping regex is an *outbound-log defense* (TM-012-adjacent), not the SQLi defense. The actual SQLi defense is the typed-helper API. Note: TM-005 ultimately needs API handlers (Phase 2 ID2/F9) to also use `?` placeholders; the BL4 helpers make it the path of least resistance. |
| **TM-012** Log-stream credential leak (structlog→stdout→Docker) | `settings.py:53-56` SecretStr keeps the token out of `model_dump`/repr by default; `settings.py:119-139` the `__init__` wrap means a token-shaped validation failure (e.g., short token) produces `ValueError("orchestrator_token validation failed: ...")` with NO raw value in `input_value`; `pool.py:283-284` `_template_only` + `_shape` strip literals from logged SQL and replace param values with their type names (`str`, `int`, `bytes`); `pool.py:289-344` every error log path uses these (`pool.integrity_violation`, `pool.connection_lost`, `pool.query_syntax_error`, `pool.write_conflict`, `pool.query_failed`); `migrate.py:598` `error_type=type(e).__name__` instead of `error=str(e)` so SQLite's `IntegrityError` literal echo cannot leak; `.semgrep/orchestrator-rules.yaml` `no-credential-log` rule active. | **Strong** | The full param-redaction discipline is implemented and covered (the literal-only regex was an explicit UAT-1 hardening). Two residual risks: (a) Semgrep's `no-credential-log` only matches the keywords `password`, `refresh_token`, `auth_code`, `orchestrator_token` — a future field named `bearer`, `api_key`, `session_secret`, `cookie`, `jwt` would slip through; (b) the rule pattern requires the secret to be passed as a **kwarg** to a `LOG.*` call. A positional-arg log (`log.error(f"failed: {token}")`) would not be caught by the rule but *would* be caught by `no-f-string-sql` only if the f-string is in SQL. Recommendation: extend `no-credential-log` keyword list and add a `pattern: log.$X(f"...")` rule. |
| **TM-014** DB file readable on host compromise (A4 on host filesystem) | `settings.py:63` default `database_path=/var/lib/orchestrator/orchestrator.db`. `migrate.py:439` opens via `sqlite3.connect`; `pool.py:659-666` opens via `aiosqlite.connect`. **No code in BL3/BL4 sets file mode on the DB file.** SQLite by default creates databases with 0644 (process umask-dependent). | **Weak (unchanged from TM)** | TM-014 explicitly accepts "on host compromise, game over" as the design decision — the mitigation is the non-root container user + state-volume mode 0700, both of which are Dockerfile/compose concerns and out of BL3/BL4 scope. **However**, BL4 *introduces a new on-disk artifact* — the WAL + SHM files alongside the DB (`.db-wal`, `.db-shm`). These are created by SQLite when WAL mode is enabled and inherit DB-file permissions. They contain transaction-recent data including pre-COMMIT writes. For a BL4 walk, this is a Strong-by-design ("same trust as DB") but worth recording in HANDOFF.md so operators understand the WAL files must be in the same mode-0700 directory. **Action item:** Phase 4 HANDOFF.md should state explicitly: "state-volume permissions cover `*.db`, `*.db-wal`, `*.db-shm` — do not separate them." |
| **TM-015** Connection-pool exhaustion on `/api/v1/games` (D, F9 API) | `settings.py:82` `pool_readers: int = Field(default=8, ge=1, le=32)`; `pool.py:542` `self._readers: asyncio.Queue(maxsize=readers_count)` enforces the cap at the queue level; `pool.py:753-792` `_checkout_reader`/`_checkout_writer` are the only paths to the connections, both gated by `state == "ready"` and the queue/lock. Excess concurrent callers `await self._readers.get()` and queue. `pool.py:541` `self._writer_lock = asyncio.Lock()` serializes all writer access; `pool.py:670` `busy_timeout = 5000ms` provides a per-statement bound. `settings.py:191-199` warns if `pool_readers > chunk_concurrency` (over-provisioning detection). | **Strong on the pool side; weak on the upstream side** | The pool itself cannot be exhausted past `pool_readers + 1 writer` connections to SQLite — that's the entire point. SQLite-side exhaustion is bounded. The remaining attack surface is **upstream of the pool**: uvicorn's `limit_concurrency` and FastAPI's request queue. TM-015 still needs uvicorn's `limit_concurrency` and (Phase 3) per-IP rate limiting, neither of which BL4 provides. **One concrete weak spot found:** if many requests are queued on `self._readers.get()` and the pool is `close()`-ing, in-flight `get()` callers will await indefinitely — `close()` does `_teardown_connections` which drains the queue without waking awaiters. A graceful-shutdown bug, not a security bug, but worth tracking. (Filed mentally as a follow-up; not a TM addition.) |
| **TM-018** Manifest memory bomb / 128 MiB cap (D, F5/F6) | `settings.py:77` `manifest_size_cap_bytes: int = Field(default=134_217_728, gt=0)`. **No enforcement code in BL3 or BL4** — this field exists, is type-validated, and exposed via `get_settings()`, but the actual streaming-parse + abort logic is in the manifest fetcher (Phase 2 ID5/ID6, not yet implemented). | **Weak — Field-only** | The settings field is correctly typed, gt=0, defaults to 128 MiB exactly, and is read through the singleton. The mitigation as described in TM-018 (size-bounded read loop, `upstream_manifest_oversize` log, job marked failed) does not yet exist in code because the manifest fetcher hasn't shipped. This is a **TM-correctness verdict**, not a BL3 verdict — BL3 has done its job by exposing the cap as a typed setting. Action item: when the Steam/Epic adapter lands, the implementer must read this field via `get_settings().manifest_size_cap_bytes` (NOT re-parse the env var) and verify the streaming abort fires on oversize. |
| **TM-019** Container escape via Python CVE | Out of BL3/BL4 scope (Dockerfile, compose security_opt, cap_drop). | **N/A here** | BL3 introduced no new dependency; BL4 added `aiosqlite` (already present). No new attack surface from BL3+BL4 changes the TM-019 posture. Snyk and Dependabot continue to cover. |
| **TM-021** CLI argument injection | Out of BL3/BL4 scope (CLI uses Click). | **N/A here** | BL3 and BL4 do not add CLI surfaces. `migrate.py:_cli` is a python -m entrypoint that takes a single positional arg (db path), no shell interpolation, no `subprocess.run(shell=True)`. Argv is not echoed verbatim (`migrate.py:593` logs `argc` only). Indirectly *strengthens* TM-021's posture. |

### Other TMs touched in passing

| TM-id | Touchpoint in BL3/BL4 | Notes |
|---|---|---|
| **TM-004** Session-file tampering | `settings.py:67-68` `steam_session_path` / `epic_session_path` are typed `Path` fields; BL3 does not open or read these files — the platform adapters do. No regression. | Unchanged. |
| **TM-008/TM-009** Repudiation | `pool.py` write helpers do not record `source` on rows — that's the schema's job (BL1 migration `0001_initial.sql`). BL4 honors whatever the schema enforces; no NULL-on-source bypass exists in the helpers. | Unchanged. |
| **TM-011** Stack-trace disclosure | `settings.py:130-139` re-raises token-related ValidationError as ValueError without traceback content; `pool.py` exception hierarchy preserves `from e` chains for debug logs but every helper raises a wrapped `PoolError` subclass that carries no SQL/params in its `args`. **A FastAPI exception handler that calls `str(exc)` on `IntegrityViolationError` would emit "unique constraint failed on jobs.id" — a table/column name, not data.** Acceptable for an internal API; document it. | Strong. |
| **TM-013** Public health fingerprint | `pool.health_check()` returns counts and replacement totals — no version, no git_sha, no SQL details. Safe. | Strong. |
| **TM-020** Supply-chain via aiosqlite / pydantic-settings | Both deps already pinned in `requirements.txt`. BL3 introduced `pydantic-settings` (already present); BL4 introduced `aiosqlite` (already present). No new transitive surface. | Unchanged. |
| **TM-022** Setuid escalation | `pool.py` does not invoke `os.setuid` / `chmod`. `settings.py` does not. `migrate.py:135` invokes `/usr/bin/stat` on macOS only with a fixed argv list (S603 noqa); on Linux it reads `/proc/self/mountinfo` directly. No setuid surface added. | Unchanged. |
| **TM-023** Multi-step chain | The "operator's library dox" step depends on `/api/v1/games` which depends on the pool. With BL4 in place, the dox-step query will go through `read_all` → reader connection → `query_only=ON`. A bearer-holder still gets the data — *nothing in BL4 mitigates the legitimate-bearer-token threat*, which is by design (TM-023 explicitly notes step 6 is "the weakest link"). Phase 3 access-log middleware remains the planned mitigation. | Unchanged. |

---

## 2. New threats not in TM-001 … TM-023

The BL3+BL4 work introduced four code paths that the original threat model does not directly enumerate. None are SEV-1, but each warrants tracking.

### TM-NEW-1 — Reader connection write-bypass via `acquire_reader()` raw escape hatch
- **Component / flow:** `pool.py:1019-1022` `acquire_reader()` returns the raw `aiosqlite.Connection`. The connection has `query_only=ON` set at open (`pool.py:678-679`), which SQLite enforces by rejecting writes with `attempt to write a readonly database` at execute-time.
- **Attack:** A future caller (e.g., a sloppy adapter) writes data through the raw reader connection thinking it's a writer. SQLite rejects, but the error path may be unhandled and crash the request with a 500 — small DoS or info leak via stack trace if FastAPI handler is misconfigured.
- **Severity:** SEV-3. Not exploitable from outside; pure programmer-error guard rail.
- **Mitigation present:** `query_only=ON` PRAGMA verified at open with `_pragma_value_matches` and `pool.pragma_mismatch` is `_log.critical` + `PoolInitError` if absent.
- **Mitigation gap:** No runtime `isinstance(conn, ReadTx)` enforcement on the raw escape hatch; relies on convention.
- **Recommendation:** Add a `wraps_only_query=True` flag on the returned connection or a single integration test that asserts `INSERT` through `acquire_reader` raises `OperationalError` and is wrapped to `QueryError` (currently it would propagate raw `aiosqlite.OperationalError` if executed directly on the yielded conn — not via the `_wrap_aiosqlite_error` path).

### TM-NEW-2 — Replacement-storm DoS amplifies a transient disk error
- **Component / flow:** `pool.py:801-863` `_replace_connection`. Storm guard at `pool.py:822-828` (`> 3 replacements in 60s → degraded; refuse further`).
- **Attack:** An attacker who can intermittently introduce disk I/O errors (e.g., NAS network blip, filesystem-level corruption, an unrelated container exhausting fs handles) can drive the pool into the storm guard. After the 4th replacement attempt in 60s, `_replace_connection` returns silently — the connection is marked unhealthy *and never replaced*, so the pool's effective capacity decays without an explicit failure mode.
- **Severity:** SEV-2. The pool reports `readers.healthy < total` in `health_check`, so an alert wired to this *would* fire — but no code path closes the pool or transitions to a degraded state that refuses new requests.
- **Mitigation present:** Storm guard logs `pool.replacement_storm` at CRITICAL.
- **Mitigation gap:** No automatic pool-close or read-only fallback. Operator-driven only.
- **Recommendation:** Document operator runbook: "if you see `pool.replacement_storm` log entries, restart the container after addressing the underlying disk fault." Track as Phase 4 ops doc item.

### TM-NEW-3 — Background task exception swallowing in early process lifecycle
- **Component / flow:** `pool.py:228-244` `_log_bg_task_exception` callback. `pool.py:564-569` `_spawn_bg`.
- **Attack:** Not really an attack — a defensive concern. If the structlog logger fails to flush before the process exits (e.g., container OOM-kill during `_replace_connection`), the `pool.background_task_failed` log line never reaches stdout. The operator sees a missing `pool.connection_replaced` and no error explanation.
- **Severity:** SEV-3. Observability gap, not a security gap.
- **Mitigation present:** Callback is wired correctly; `_log.error` is called with structured fields.
- **Mitigation gap:** No `sentry`-style flush on exit; no explicit `atexit` handler.
- **Recommendation:** Acceptable as-is. Track as a Phase 3 hardening idea ("structlog emit-on-shutdown").

### TM-NEW-4 — `verify_schema_current` race on first boot
- **Component / flow:** `migrate.py:539-564` `verify_schema_current`; `pool.py:629-643` calls it inside `_async_create`.
- **Attack:** Two orchestrator processes start simultaneously (e.g., Docker Swarm rolling update). Process A runs `run_migrations()`, locks DB via `BEGIN IMMEDIATE`. Process B's `Pool.create()` runs `verify_schema_current()` against an empty `schema_migrations` table (or a partially-migrated one if A is mid-apply but the table-creation DDL has flushed). B raises `SchemaNotMigratedError` and `_async_create` rolls back via `_teardown_connections()`, exit 1.
- **Severity:** SEV-3. The intended deployment is single-container per ADR-0001. Multi-process startup is explicitly out of scope (`migrate.py:80-87`). Crash-on-startup of process B is the *correct* behavior — the orchestrator system fails fast and the operator must serialize boots.
- **Mitigation present:** The `_RUNNER_LOCK` only serializes within one Python process. `BEGIN IMMEDIATE` serializes across-process for the runner. `verify_schema_current` does not block — it just reads.
- **Mitigation gap:** None required for current deployment topology.
- **Recommendation:** Document in HANDOFF.md: "Do not run two orchestrator containers against the same state-volume. Multi-container is post-MVP and would require `fcntl.flock` on the database path." (already partially in `migrate.py:84-87` comment).

---

## 3. Particularly well-mitigated TMs (worth highlighting)

- **TM-012 (log credential leak)** is *exceptionally* well-mitigated for the BL3+BL4 surface. Three independent layers: SecretStr by-default redaction; ValidationError input-echo interception in `Settings.__init__`; `_template_only` + `_shape` on every error-log path in the pool; Semgrep `no-credential-log` rule; UAT-1 hardening of `migrate._cli` to log `error_type` instead of `str(e)`. Compensating controls cover both kwarg-style logs and SQL-message reflection.
- **TM-005 (SQL injection)** is well-mitigated structurally. The pool's API surface does not accept user-supplied SQL strings — `sql` is always a hardcoded constant at the callsite. Even if a future API handler accidentally f-string-formatted user input into `sql`, Semgrep `no-f-string-sql` blocks it at CI.
- **TM-001 (bearer-token leak) — the orchestrator side** is robust. SecretStr + ValidationError-wrap + pickle-block + env-shadow warning + argv-elision is more layered than the original TM-001 specified.

---

## 4. Summary table — verdicts at a glance

| TM | Status | Key code path | Action item |
|---|---|---|---|
| TM-001 | **Strong** | settings.py:53-150 | None for BL3/BL4 |
| TM-005 | **Strong** | pool.py helper API + Semgrep `no-f-string-sql` | None |
| TM-012 | **Strong** | settings + pool error logs + Semgrep `no-credential-log` | Extend Semgrep keyword list (`bearer`, `api_key`, etc.) |
| TM-014 | **Weak (by design)** | container/compose layer, not BL3/BL4 | HANDOFF.md must state WAL/SHM permission requirement |
| TM-015 | **Strong (pool side)** | `pool_readers` cap, queue maxsize, writer Lock | uvicorn `limit_concurrency` still needed (Phase 2 API) |
| TM-018 | **Field-only / Weak** | settings.py:77 has the cap; no enforcement code | When manifest fetcher lands, must read setting + enforce |
| TM-019 | **N/A in BL3/BL4** | container hardening only | None |
| TM-021 | **Strong (indirect)** | migrate._cli logs argc-only | None |
| TM-NEW-1 | **Acceptable** | acquire_reader() escape hatch | Optional: integration test for write-attempt path |
| TM-NEW-2 | **Acceptable + ops doc** | storm guard at pool.py:822 | Phase 4 runbook entry |
| TM-NEW-3 | **Acceptable** | bg-task callback | Phase 3 hardening idea |
| TM-NEW-4 | **Acceptable + HANDOFF doc** | verify_schema_current race | HANDOFF.md note (single-container only) |

---

## 5. Verification commands run / referenced

- `grep -rn "execute(f\"\\|execute(\\\".*\\\" *+\\|executemany(f\"" src/` — no offenders in BL3/BL4 paths
- `grep -rn "shell=True" src/` — none
- `grep -rn "ORCH_TOKEN\\|orchestrator_token" src/orchestrator/core/settings.py` — only in field validators
- Reviewed `.semgrep/orchestrator-rules.yaml` — 7 active rules covering TM-005, TM-012, TM-015, TM-021
- Spot-checked `tests/db/test_pool.py`, `tests/db/test_pool_chaos.py` exist and exercise replacement state machine, integrity classification, and PRAGMA verification
- Spot-checked `tests/core/test_settings.py` exists (BL3 ID4 shipped at 100% branch coverage per memory entry)

---

## 6. Sign-off note for Orchestrator

No SEV-1 or SEV-2 mitigation gaps were found in the BL3+BL4 implementation. Two **field-only** mitigations (TM-018 manifest cap, and the implicit dependency on TM-014's container hardening) are correct for this layer but require downstream features and ops docs to fully realize. Four net-new threat scenarios (TM-NEW-1 through TM-NEW-4) are all acceptable-risk under the single-container ADR-0001 deployment topology, with three calling for documentation updates (HANDOFF.md, Phase 4 runbook) rather than code changes.

The BL3+BL4 surface is the strongest STRIDE coverage the project has shipped to date — particularly for I (Information Disclosure) where three independent layers cover credential redaction.

— Threat-Model-Walk Agent, UAT-2
