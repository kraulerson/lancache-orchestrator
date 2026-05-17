# UAT Test Session — 2 (v1)

**Date:** 2026-04-26
**Features Under Test:** BL3 ID4 Settings module + BL4 DB pool
**Tester:** Karl (Orchestrator)
**Format:** H-1 lightweight (foundational modules, no user-facing surfaces yet)

---

## Instructions

1. Run each scenario from the project root inside the venv: `source .venv/bin/activate`.
2. Mark Pass/Fail per row.
3. If Fail: fill in the Bugs Found table at the bottom.
4. When done, save this file to `tests/uat/sessions/2026-04-26-session-2/submissions/test-session-2-v1.md` and tell the Orchestrator agent "results are in".

---

## Pre-flight

| # | Check | Command | Expected |
|---|---|---|---|
| P1 | venv active | `which python` | `.../lancache_orchestrator/.venv/bin/python` |
| P2 | branch | `git branch --show-current` | `feat/uat-2-session` |
| P3 | clean tree | `git status --short` | (only `?? .claude/settings.json.pre-wire-backup` if not deleted) |
| P4 | unit test baseline | `pytest tests/db/ tests/core/ -q` | All pass; ~ 184 tests |

Pre-flight all-pass: ☐

---

## Scenario 1 — Settings construction with valid env

| Step | Command / action | Expected |
|---|---|---|
| 1.1 | `export ORCH_TOKEN=$(printf 'a%.0s' {1..32})` | (no output) |
| 1.2 | `python -c "from orchestrator.core.settings import get_settings; s = get_settings(); print(s.api_host, s.api_port, s.pool_readers)"` | `127.0.0.1 8765 8` |
| 1.3 | `python -c "from orchestrator.core.settings import get_settings; print(repr(get_settings().orchestrator_token))"` | `SecretStr('**********')` (10 stars; never the raw token) |

Pass / Fail: ☐

---

## Scenario 2 — Diagnostic warnings fire on misconfiguration

| Step | Command / action | Expected |
|---|---|---|
| 2.1 | `export ORCH_API_HOST=0.0.0.0 ORCH_CORS_ORIGINS='["*"]'` | (no output) |
| 2.2 | `python -c "from orchestrator.core.settings import reload_settings; reload_settings()" 2>&1` | TWO log lines containing `config.api_bound_non_loopback` and `config.cors_wildcard` (JSON format) |
| 2.3 | Reset: `unset ORCH_API_HOST ORCH_CORS_ORIGINS` | (no output) |

Pass / Fail: ☐

---

## Scenario 3 — Run migrations on a fresh DB

| Step | Command / action | Expected |
|---|---|---|
| 3.1 | `tmp_db=$(mktemp -t orch-uat2.XXXXXX); rm -f $tmp_db; echo $tmp_db` | a path like `/tmp/orch-uat2.XXXXXX` |
| 3.2 | `python -m orchestrator.db.migrate "$tmp_db"` | logs `migrations_complete applied_count=1`; exit 0 |
| 3.3 | `sqlite3 "$tmp_db" "SELECT id, name FROM schema_migrations"` | `1\|initial` |
| 3.4 | `sqlite3 "$tmp_db" "PRAGMA journal_mode"` | `wal` |

Pass / Fail: ☐

---

## Scenario 4 — Pool init from Settings + basic query

| Step | Command / action | Expected |
|---|---|---|
| 4.1 | `export ORCH_DATABASE_PATH="$tmp_db"` (using path from Scenario 3) | (no output) |
| 4.2 | Run inline:<br>`python -c "import asyncio; from orchestrator.db.pool import init_pool, close_pool; from orchestrator.core.settings import reload_settings; reload_settings(); async def m():`<br>`    p = await init_pool();`<br>`    print(await p.read_one('SELECT name FROM platforms WHERE name=?', ('steam',)));`<br>`    await close_pool();`<br>`asyncio.run(m())"` | `{'name': 'steam'}` |

(If multi-line python is awkward, the equivalent script is at `/tmp/uat2-scenario4.py` — see appendix below.)

Pass / Fail: ☐

---

## Scenario 5 — Reader query_only enforcement

| Step | Command / action | Expected |
|---|---|---|
| 5.1 | (Pool still up from S4 OR re-run init.) Use raw acquire on a reader and try to write. | A clear `OperationalError` containing `"readonly"` or similar, NOT a silent success. |
| 5.2 | Run script `/tmp/uat2-scenario5.py` (see appendix). | Script prints `BLOCKED: ...readonly...` and exits 0. |

Pass / Fail: ☐

---

## Scenario 6 — schema_status + health_check shapes

| Step | Command / action | Expected |
|---|---|---|
| 6.1 | Run `/tmp/uat2-scenario6.py`. | `schema_status` dict has keys `applied`, `available`, `pending`, `unknown`, `current` and `current` is `True`. `health_check` dict has `writer.healthy: True`, `readers.total: 8`, `readers.healthy: 8`, `uptime_sec >= 0`. |

Pass / Fail: ☐

---

## Scenario 7 — Bad env var rejected

| Step | Command / action | Expected |
|---|---|---|
| 7.1 | `ORCH_TOKEN='short' python -c "from orchestrator.core.settings import reload_settings; reload_settings()" 2>&1` | A `ValueError` whose message does NOT echo `'short'` (it should be scrubbed). Exit code non-zero. |
| 7.2 | `ORCH_POOL_READERS=99 python -c "from orchestrator.core.settings import reload_settings; reload_settings()" 2>&1` | A `ValidationError` mentioning `pool_readers` and `le=32` (or similar bound). |

Pass / Fail: ☐

---

## Scenario 8 — Cleanup

| Step | Command / action | Expected |
|---|---|---|
| 8.1 | `rm -f "$tmp_db"` | (no output) |
| 8.2 | `unset ORCH_TOKEN ORCH_DATABASE_PATH` | (no output) |

Pass / Fail: ☐

---

## Appendix — helper scripts

The Orchestrator agent will write these to `/tmp` before you start:

- `/tmp/uat2-scenario4.py` — Pool init + simple read
- `/tmp/uat2-scenario5.py` — query_only enforcement probe
- `/tmp/uat2-scenario6.py` — schema_status + health_check inspection

Each script is < 30 lines, prints clear PASS / FAIL output, exits 0/1 accordingly.

---

## Bugs Found

| # | Severity | Feature | Description | Steps to Reproduce | Expected vs Actual |
|---|---|---|---|---|---|
| | SEV-? | | | | |

### Severity Guide
- **SEV-1:** Data loss, security breach, app crash on core flow
- **SEV-2:** Feature broken but workaround exists, significant UX failure
- **SEV-3:** Minor UX issue, cosmetic, non-core edge case
- **SEV-4:** Enhancement, suggestion, polish

---

## Overall Notes

_Free-form observations, UX concerns, surprises._
