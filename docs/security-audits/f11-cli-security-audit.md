# Security Audit — F11 `orchestrator-cli`

**Date:** 2026-06-08
**Scope:** `src/orchestrator/cli/` — `client.py`, `output.py`, `base.py`, `main.py`, `commands/{auth,library,status,game,jobs,db,config}.py`; the `pyproject.toml` entry point.
**Persona:** Senior Security Engineer. Two 4-lens adversarial workflows (secret-leakage, exit-code mapping, HTTP correctness, robustness) were run — one over the initial batch, one over the remediated batch — and each finding was skeptic-verified before fixing.

## Threat review

| Vector | Assessment |
|--------|------------|
| **Credential / token disclosure** | The Steam username/password, Steam Guard code, and Epic authorization code are read via `click.prompt(..., hide_input=True)` for the secrets (password/codes) and sent only in the request body — **never echoed, logged, or placed in any output/error string**. The bearer `ORCH_TOKEN` is read from the environment and only ever set as the `Authorization: Bearer` header in `client.py`; it is not logged and does not appear in any rendered output. `config show` redacts secret-bearing fields **by name** (`token`/`secret`/`password`), covering both the `orchestrator_token` `SecretStr` and the plain-`str` `epic_client_secret` (Finding #2) — verified no raw value prints. |
| **Error-message reflection** | `ApiError` surfaces the server's `detail` field. The server's routers never echo a submitted secret in `detail` (auth failures return generic `"authentication failed: <kind>"`). The one reflection path — FastAPI's default validation-error payload echoing the raw `input` (Finding #1) — is closed: `_validation_error_handler` now strips `input`/`ctx`/`url`. So no submitted credential reflects back through a CLI error. |
| **Local privilege (db/config)** | `db migrate|vacuum` and `config show` run **in-process** and are deliberately **not** exposed via the REST API — schema/maintenance ops never become an HTTP attack surface. They operate only on the operator-configured `database_path`. |
| **SQL injection** | The only SQL the CLI issues is the constant `"VACUUM"` (via aiosqlite); migrations run through the existing, checksum-pinned runner. No interpolation, no f-string SQL. |
| **Transport** | The CLI targets the loopback API (`http://127.0.0.1:8765` default) with a 5 s connect / 30 s read timeout; no redirects followed; connection failures map to a clean exit 2, not a stack trace. |
| **Exit-code contract** | Failures are mapped to the Manifesto's codes (API unreachable → 2, auth → 3, other → 1) via `handles_api_errors`; no failure path silently exits 0. |

## Findings

The 4-lens adversarial review surfaced defects that were each skeptic-verified, then **fixed in-batch test-first**. The security-relevant ones:

| # | Sev | Finding | Fix | Regression test |
|---|-----|---------|-----|-----------------|
| 1 | SEV-2 | **Credential reflection.** FastAPI's default `RequestValidationError` handler returned `exc.errors()`, which includes the rejected `input` (the raw request body). A malformed `POST /platforms/steam/auth` (e.g. username omitted) reflected the **submitted password** back in the 400 response — and into any log capturing it. | `api/main.py` `_validation_error_handler` now keeps only `type`/`loc`/`msg` per error, dropping `input`/`ctx`/`url`. Closes the UAT-10 deferred "validation-error input echo" item. | `test_auth_router.py::test_validation_error_does_not_reflect_submitted_password` |
| 2 | SEV-3 | **`config show` leaked `epic_client_secret`.** Redaction relied on `SecretStr` self-masking, but `epic_client_secret` is a plain `str` (the public legendary client secret) — it printed raw. Type-only redaction misses any future plain-`str` secret. | `config show` redacts by **field name** (`token`/`secret`/`password` markers), independent of type. | `test_cmd_config.py::test_config_show_redacts_secret_named_fields` |

Robustness/correctness defects from the same review (non-secret) were also fixed test-first: every `httpx.TransportError` subclass now maps to exit 2 (was a narrow 3-tuple that let `PoolTimeout`/`WriteError`/`RemoteProtocolError` escape as a traceback); `/health` 503-degraded body now renders via `OrchClient.get_health()` instead of raising `ApiError`; `output.table()` tolerates ragged rows; and a `handles_api_errors` backstop turns a malformed 2xx body into a clean exit 1.

A **second** adversarial pass over the remediated batch surfaced two more confirmed (skeptic-verified) defects, also fixed test-first:

| # | Sev | Finding | Fix | Regression test |
|---|-----|---------|-----|-----------------|
| 3 | SEV-2 | **`db migrate`/`db vacuum` escaped raw tracebacks.** These run in-process (no API), so `handles_api_errors` doesn't apply — and `MigrationError`/`sqlite3.Error`/`OSError` aren't in its backstop. An unopenable DB path or a non-SQLite file printed a multi-frame stacktrace, bypassing the F11 clean-error contract. | New `handles_db_errors` decorator (catches `MigrationError`, `aiosqlite.Error`, `OSError` → `✗` + exit 1) on both commands. `aiosqlite.Error` (== `sqlite3.Error`) avoids a raw `sqlite3` import that would trip the `no-sync-sqlite` semgrep rule. | `test_cmd_db.py::test_db_migrate_open_failure_exits_1_cleanly`, `::test_db_vacuum_on_non_sqlite_file_exits_1_cleanly` |
| 4 | SEV-3 | **Malformed `--url` escaped as a traceback.** `httpx.Client(base_url=...)` parses the URL eagerly in the constructor; a control char raises `httpx.InvalidURL`, which is **not** a `TransportError`, so the exit-2 mapping missed it. | `_request` now catches `(httpx.TransportError, httpx.InvalidURL)`. | `test_client.py::test_malformed_base_url_maps_to_unreachable` |

Two findings from the second pass were skeptic-**refuted** (not live defects): `output.table()` dropping cells in rows *longer* than the header (intended — there is no column to render them under) and `status_label()` raising on a non-`str` (only ever called with `str`/`Literal` values). One finding was confirmed but is the **already-documented F11 cutline limitation**: `game show` filters only the first 500 games (no `GET /games/{id}` detail endpoint — deferred via #141), and the truncation is disclosed in the not-found message.

**0 open security findings.** No new dependency (click/httpx/aiosqlite already direct deps), no SQL interpolation, no secret reaches stdout/stderr/logs after the fixes above.

## Residual / accepted
- `--json` machine output, `game block|unblock` (+ the F8 block-list API), and a `GET /games/{id}` detail endpoint (#141) are **deferred** (the spec's deferred list); `game show` works around the missing detail route by filtering the list.
- `config show` prints every effective setting (secrets redacted) — intended for operator diagnosis; no secret-bearing field other than `orchestrator_token` exists (the Epic client_id/secret are the public legendary creds).
