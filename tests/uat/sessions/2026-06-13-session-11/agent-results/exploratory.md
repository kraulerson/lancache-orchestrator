# UAT-11 — Exploratory (Malicious-User / Skeptical-Operator)

- **Surface focus:** operator experience of user-facing surfaces — `orchestrator-cli`, the REST API error/UX surface (read), and operator docs.
- **Method:** read the CLI + router + settings code AND ran `orchestrator-cli` offline (API intentionally down) against bad args, missing/malformed env, and bad DB paths. No real secrets used (dummy 32-char hex token).
- **Out of scope (per brief):** internal code defects already covered by the just-completed 14-subsystem audit. Reported here only where they surface as an *operator-visible* foot-gun.

Branch: `main`. Date: 2026-06-13.

---

## Severity summary

| Severity | Count |
|---|---|
| SEV-1 | 0 |
| SEV-2 | 1 |
| SEV-3 | 4 |
| SEV-4 | 3 |
| nit | 3 |

---

## SEV-2

### S11-E-01 — `config show` / `db migrate` / `db vacuum` traceback when `ORCH_TOKEN` is unset
- **Surface:** CLI (`config show`, `db migrate`, `db vacuum` — all in-process commands)
- **Repro:**
  ```
  unset ORCH_TOKEN
  orchestrator-cli config show
  orchestrator-cli db migrate
  ```
- **Observed:** A raw ~15-frame Python traceback ending in
  `ValueError: orchestrator_token validation failed: Field required`, plus a
  `UserWarning: directory "/run/secrets" does not exist` on stderr. Exit 1.
- **Expected:** A single clean `✗ orchestrator_token validation failed: Field required`
  line (the F11 clean-error contract that every other command honours).
- **Root cause (operator-relevant):** `Settings.__init__` deliberately converts a
  token `ValidationError` into a plain `ValueError` (to scrub the secret from the
  message — see `settings.py:310`). But the in-process guard
  `handles_local_errors` (`cli/base.py:68`) only catches
  `(ValidationError, MigrationError, aiosqlite.Error, OSError)` — **not `ValueError`** —
  so the scrubbed re-raise escapes as a traceback.
- **Why untested:** the CLI test harness `tests/cli/conftest.py:34` defaults every
  invoke to `env={"ORCH_TOKEN": "t"}`, and `test_config_show_malformed_env_exits_1_cleanly`
  *also* sets `ORCH_TOKEN` (it only exercises the malformed-*port* `ValidationError`
  path). The missing-token `ValueError` path is never exercised.
- **Operator impact:** This is the *first thing* a new operator hits — running
  `config show` or `db migrate` before exporting the token. Instead of a one-line
  hint, they get a stack trace that looks like a crash. Directly violates the
  documented F11 "clean error, not a raw traceback" promise.
- **Fix sketch:** add `ValueError` to the `handles_local_errors` except tuple (or
  have settings raise a typed error the guard already catches).

---

## SEV-3

### S11-E-02 — Bad CLI argument and "API unreachable" both exit 2 (indistinguishable to scripts)
- **Surface:** CLI exit-code contract (Manifesto F11: 2 = unreachable, 3 = auth, 1 = other)
- **Repro:**
  ```
  orchestrator-cli game list --limit abc   # → exit 2 (Click usage error)
  orchestrator-cli frobnicate               # → exit 2 (unknown command)
  orchestrator-cli status                   # (API down) → exit 2 (unreachable)
  ```
- **Observed:** All three exit 2. Click's built-in usage-error exit code is 2, which
  **collides** with the orchestrator's "API unreachable = 2" code.
- **Expected:** A script wrapping the CLI cannot tell "the orchestrator is down"
  (retry/alert) from "I passed a malformed argument" (operator error, don't retry).
  The brief explicitly calls out exit-code correctness as a target.
- **Operator impact:** Any automation/cron/healthcheck around the CLI that branches on
  exit 2 will mis-handle typos as outages and vice-versa.
- **Note:** This is inherent to layering the F11 codes on top of Click's defaults; worth
  a documented caveat at minimum, or remapping unreachable to a non-2 code.

### S11-E-03 — Wrong Steam/Epic credential reports "check ORCH_TOKEN" (server detail discarded)
- **Surface:** CLI `auth steam` / `auth epic` + client 401 handling
- **Repro (by inspection; needs live API to run):** `orchestrator-cli auth steam`,
  enter a valid `ORCH_TOKEN` but a **wrong Steam password**. The API returns
  `401 {"detail": "authentication failed: <kind>"}` (`api/routers/auth.py:192`).
- **Observed:** The CLI client catches *any* 401 and raises a hardcoded
  `AuthError("authentication failed — check ORCH_TOKEN")` (`cli/client.py:68-69`),
  **discarding the server's detail body** and exiting 3.
- **Expected:** When the operator is mid-`auth steam`, the failure is the *Steam*
  credential, not `ORCH_TOKEN` — which was accepted. Telling them to "check ORCH_TOKEN"
  sends them debugging the wrong thing.
- **Fix sketch:** surface the server `detail` when present (e.g.
  `authentication failed — <detail>`) instead of the fixed ORCH_TOKEN string, or
  special-case the auth commands.

### S11-E-04 — Invalid `--state` / `--kind` / `--status` silently returns an empty table
- **Surface:** CLI `jobs` / `game list` + API filter parsing
- **Repro:**
  ```
  orchestrator-cli jobs --state success      # valid value is "succeeded"
  orchestrator-cli jobs --kind sync          # valid value is "library_sync"
  orchestrator-cli game list --status uptodate
  ```
- **Observed:** Options are free `TEXT` (no `click.Choice`), and the API filter
  allow-list validates the *field* and *op* but treats the *value* as an opaque string
  (`value_type=str`). An out-of-domain value passes validation and returns an empty
  result set with **no error and no hint**.
- **Expected:** A typo'd enum value should either be rejected ("unknown state
  'success'; valid: queued|running|succeeded|failed|cancelled") or at minimum
  constrained client-side with `click.Choice`. The closed sets exist in code
  (`jobs.py:84-89`: kinds, states, platforms) but aren't surfaced.
- **Operator impact:** Operator concludes "no matching jobs" when really they mistyped
  the filter — a classic silent foot-gun for a status/triage tool.

### S11-E-05 — `game show -<n>` reports "No such option" instead of "invalid id"
- **Surface:** CLI `game show`
- **Repro:** `orchestrator-cli game show -5`
- **Observed:** `Error: No such option '-5'.` (Click parses the leading `-` as an option
  flag before the int converter runs). Exit 2.
- **Expected:** A message about the *argument value* (e.g. "GAME_ID must be a positive
  integer"), not a misleading "no such option". An operator pasting a negative or
  signed id gets a nonsensical error.
- **Fix sketch:** validate id `>= 1` via a callback, or document that ids are positive.

---

## SEV-4

### S11-E-06 — `epic_token_url` redacted in `config show` (it's a URL, not a secret)
- **Surface:** CLI `config show`
- **Repro:** `ORCH_TOKEN=<32hex> orchestrator-cli config show | grep epic_token_url`
- **Observed:** `epic_token_url   **********`. The OAuth *endpoint URL* is masked by the
  `"token" in name` substring rule (`cli/config.py:13`), which over-matches.
- **Expected:** The endpoint URL is operator-debugging information (analogous to
  `epic_library_url`, `epic_manifest_url_template`, which *are* shown). Hiding it makes
  "why is Epic auth failing / hitting the wrong endpoint" harder to diagnose.
- **Note:** `epic_client_secret` and `orchestrator_token` are correctly redacted; this
  is a false-positive only. Redact by an explicit secret-field allow-list rather than a
  substring.

### S11-E-07 — `UserWarning: directory "/run/secrets" does not exist` leaks on every in-process command
- **Surface:** CLI `config show` / `db migrate` / `db vacuum` (anything that builds `Settings` off-host)
- **Repro:** `ORCH_TOKEN=<32hex> orchestrator-cli config show` on a host without
  `/run/secrets` (i.e. any dev/laptop run).
- **Observed:** pydantic-settings prints
  `UserWarning: directory "/run/secrets" does not exist` to stderr before the real
  output, on every invocation.
- **Expected:** Clean output. The Docker-secrets dir is expected to be absent off the
  deployment host; the warning is noise that makes the operator think something is
  wrong. Suppress it when the secrets dir is intentionally optional.

### S11-E-08 — `db vacuum` error omits the DB path that `db migrate` includes
- **Surface:** CLI `db vacuum` vs `db migrate`
- **Repro:**
  ```
  ORCH_DATABASE_PATH=/no/such/db.sqlite orchestrator-cli db migrate
    → ✗ Failed to open database at /no/such/db.sqlite: unable to open database file
  ORCH_DATABASE_PATH=/no/such/db.sqlite orchestrator-cli db vacuum
    → ✗ unable to open database file
  ```
- **Observed:** `db vacuum` surfaces the bare sqlite message with **no path**;
  `db migrate` includes the path. Inconsistent and less actionable.
- **Expected:** Both should name the offending path.

---

## Nits

### S11-E-09 — `python -m orchestrator.cli.main` silently does nothing (no `__main__` guard)
- **Surface:** CLI entry point
- **Repro:** `python -m orchestrator.cli.main status` → no output, exit 0.
- **Detail:** `cli/main.py` has no `if __name__ == "__main__": main()` guard, so the
  `python -m` form (which a runbook or this very UAT brief suggests) is a no-op. Only the
  installed console script `orchestrator-cli` works. Add the guard so both forms behave.

### S11-E-10 — `config show` column overflows the 34-char pad for long keys
- **Surface:** CLI `config show` formatting
- **Detail:** `f"{key:34}"` (`cli/config.py:36`) is exceeded by keys like
  `scheduler_library_sync_interval_sec` and
  `steam_worker_library_enumerate_timeout_sec`, leaving only one space before the value —
  values no longer align in a column. Cosmetic; use a computed max width.

### S11-E-11 — `--limit` help advertises no maximum (operator learns the 500 cap by trial)
- **Surface:** CLI `game list` / `jobs` help
- **Detail:** `--limit INTEGER [default: 50]` with no documented max. `--limit 999999999`
  is accepted client-side, sent, and rejected by the API with
  `✗ HTTP 400: limit must be <= 500, got 999999999`. Functionally correct (good API
  message), but the cap should be in `--help`.

---

## What works well (positive observations)
- **Unreachable path is clean and consistent:** `status`, `game list`, `jobs`, and a
  malformed `--url` all give `✗ orchestrator API not reachable at <url>` + exit 2 — no
  tracebacks (`client.py` catches `TransportError`+`InvalidURL`).
- **`/health` degraded representation is correct:** `status` renders the 503-with-body
  degraded state instead of erroring (`get_health(ok_extra=(503,))`).
- **Colorblind-safe output is real:** icon + UPPERCASE label, never color alone
  (`output.py`); unmapped states fall back to a neutral dot, never `KeyError`.
- **Secret redaction holds for actual secrets:** `orchestrator_token`,
  `epic_client_secret` (a plain `str`) both masked; `git_sha` truncated to 8 chars on the
  unauthenticated `/health`.
- **API 4xx messages are actionable:** `game X not found`,
  `validate only supports steam (got 'epic')`, `limit must be <= 500` — all clear.
- **DB pool failures degrade to 503 `{"detail":"database unavailable"}`** rather than
  leaking internals.

---

## Top operator-experience gap (cross-cutting)
**The CLI is undocumented for operators.** `orchestrator-cli` appears **zero times** in
both `README.md` and `docs/reference/user-guide.md` (the latter is the *framework's*
generic guide, not a product operator guide). There is no documentation of: how to invoke
it (the console script vs the dead `python -m` form), that `ORCH_TOKEN` must be exported
for it (README ties the token only to the *server*), the exit-code contract
(2/3/1), or the valid `--state`/`--kind`/`--status` enum values. An operator handed this
tool has to read source to use it. (Filed as the doc context for the findings above
rather than a numbered SEV; recommend a "Operator CLI" section in README.)
