# CLI (F11) — `orchestrator-cli` — Design

**Date:** 2026-06-07
**Feature:** F11 (PRODUCT_MANIFESTO §50, §202; journey §93–§96) — operator command-line interface.
**Status:** Approved design → implementation plan next.

## Goal

A Click-based `orchestrator-cli`, bundled in the container, that lets the operator authenticate platforms, trigger syncs/prefills/validations, and inspect state — by calling the orchestrator's **local REST API** with the same bearer token. It is the operator's primary hands-on surface (the status page is read-only HTML; Game_shelf is the rich UI).

## Locked decisions (Manifesto F11 — not re-litigated here)

- **Click-based** (`click==8.4.0`, already a direct dep). Entry point `orchestrator-cli = "orchestrator.cli.main:cli"` is already declared in `pyproject.toml`; `src/orchestrator/cli/` exists (empty).
- **Hits the local REST API over HTTP** with the same bearer token (not in-process / direct-DB) — **except** `db` and `config`, which have no API endpoint and run locally in-process.
- **No `--json`** (deferred Post-MVP, OQ6) — human-readable output only.
- **Colorblind-safe output** (Intake §9): every status indicator uses **icon + text label**, never color alone.
- **Exit codes:** API unreachable → **2**; auth failure (401) → **3**.

## Approved scope decisions

- **Full F11 in one feature:** `auth steam|epic|status`, `library sync`, `status`, `game list|show|prefill|validate|manifest`, `jobs`, `db migrate|vacuum`, `config show`.
- **`game show <id>` filters the list endpoint** (there is no `GET /games/{id}` — UAT-10 #141); no API change.
- **`game block|unblock` deferred** to when the F8 block-list API ships (out of scope).

## Architecture

New `src/orchestrator/cli/` package, one focused module per concern:

- **`main.py`** — the root `cli` `click.Group`. Global options: `--url` (default `http://127.0.0.1:8765`, env `ORCH_API_URL`) and the bearer token from env `ORCH_TOKEN`. Registers the subcommand groups. Top-level `try/except` maps `OrchClientError` subtypes to exit codes 2/3 and other failures to 1.
- **`client.py`** — `OrchClient`: a thin synchronous `httpx.Client` wrapper. `__init__(base_url, token)`; methods `get(path, **params)`, `post(path, json=None)` returning parsed JSON. Sets `Authorization: Bearer <token>`. Raises `ApiUnreachable` (→ exit 2) on `httpx.ConnectError`/timeout, `AuthError` (→ exit 3) on 401, `ApiError` (→ exit 1) on other non-2xx (with the server's `detail`). Sync is correct — Click is sync; the server async-ness is irrelevant to an HTTP client.
- **`output.py`** — colorblind-safe rendering helpers: `status_label(value) -> "<icon> <TEXT>"` (e.g. `ok → "✓ OK"`, `expired → "✗ EXPIRED"`, `validation_failed → "⚠ VALIDATION_FAILED"`), a minimal fixed-width `table(rows, headers)`, and `success`/`warn`/`error` line printers (icon + text, no bare color). Uses `click.echo`.
- **`commands/`** — `auth.py`, `library.py`, `status.py`, `game.py`, `jobs.py`, `db.py`, `config.py`, each exposing a `click.Group`/commands wired into `cli` in `main.py`.

### Command → endpoint map

| Command | Action |
|---------|--------|
| `auth steam` | Prompt username + password (hidden) → `POST /api/v1/platforms/steam/auth`. On `202` (challenge), prompt the Steam Guard code → `POST /api/v1/platforms/steam/auth/{challenge_id}`. Print SUCCESS + the Valve "new-device email" warning (X7). On `200` (no 2FA) finish immediately. |
| `auth epic` | Prompt the `legendary.gl/epiclogin` authorization code → `POST /api/v1/platforms/epic/auth`. Print SUCCESS (account/display name; never echo the code/token). |
| `auth status` | `GET /api/v1/platforms` → table of steam/epic `auth_status` (icon+text) + `last_sync_at`, `last_error`. |
| `library sync [--platform steam\|epic]` | `POST /api/v1/platforms/{platform}/library/sync` (default steam) → print the returned `job_id`. |
| `status` | Aggregate `GET /api/v1/health` + `GET /api/v1/platforms` → a colorblind-safe summary (scheduler/validator/cache/lancache booleans + per-platform auth). |
| `game list [--platform --status --limit]` | `GET /api/v1/games` (+ filters) → table (id, platform, app_id, title, status[icon+text]). |
| `game show <id>` | `GET /api/v1/games?...&limit=...`, find the row with that id client-side → detail block (no detail endpoint exists). Exit 1 with a clear message if not found. |
| `game prefill <id>` | `POST /api/v1/games/{id}/prefill` → print job_id. |
| `game validate <id>` | `POST /api/v1/games/{id}/validate` → print job_id. |
| `game manifest <id>` | `POST /api/v1/games/{id}/manifest/fetch` → print job_id. |
| `jobs [--kind --state --limit]` | `GET /api/v1/jobs` (+ filters) → table (id, kind, platform, state[icon+text], progress, error). |
| `db migrate` | **Local:** call the in-process migration runner (`orchestrator.db.migrate.run_migrations`) against `settings.database_path`; print applied count. |
| `db vacuum` | **Local:** open the DB and run `VACUUM`; print reclaimed/OK. |
| `config show` | **Local:** `get_settings().model_dump()` with `SecretStr` redacted (already redacts) → key/value listing of the effective config. |

### Auth interactive flow (the one stateful command)

`auth steam` is the only multi-step command: begin → (optional 2FA challenge) → complete. It prompts via `click.prompt(..., hide_input=True)` for the password and the code; credentials are RAM-only (IS3) and never logged. A non-202/200 response → `AuthError`/`ApiError` with the server `detail`. (The 120 s mobile-approval timeout, X3, is the server's concern; the CLI surfaces whatever the server returns.)

## Error handling / exit codes

- `httpx.ConnectError` / connect timeout → `ApiUnreachable` → **exit 2**, message: "orchestrator API not reachable at <url> — is the container running?".
- `401` → `AuthError` → **exit 3**, message: "authentication failed — check ORCH_TOKEN".
- other non-2xx → `ApiError` → **exit 1**, message includes the server's `detail`.
- success → **0**.
- Missing `ORCH_TOKEN` → exit 3 with guidance (HTTP commands only; `db`/`config` don't need it).

## Testing

- **HTTP commands:** Click `CliRunner` + `httpx.MockTransport` injected into `OrchClient` (a `_build_transport()` seam mirroring the project's existing pattern). Assert the request (path/method/body/bearer header), the rendered output, and the exit code. Cover: `auth steam` 200-path and 202→200 path (simulated `input`), `auth status`/`status`/`game list`/`jobs` rendering, `library sync`/`game prefill|validate|manifest` job_id echo, `game show` found + not-found.
- **Error/exit codes:** ConnectError → 2; 401 → 3; 500 `detail` → 1.
- **Local commands:** `db migrate` against a temp DB asserts migrations applied; `db vacuum` runs cleanly; `config show` redacts the token and lists fields.
- **Output:** `status_label` returns icon+text for every `games.status` / `auth_status` value (colorblind-safe, no bare color).
- Conventions: `.venv/bin/pytest`; `ruff`/`mypy --strict src/`; no live network.

## Deferred (follow-ups)

- `--json` machine output (OQ6). `game block|unblock` + the F8 block-list API. `GET /api/v1/games/{id}` detail endpoint (#141) — `game show` works around it. Fuzzy title search (FG5).
