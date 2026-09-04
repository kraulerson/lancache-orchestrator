# Cache Validation Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make cache truth un-corruptible by job failures, make the sweep able to reach every game, and alarm on mass cache loss before it costs a re-download.

**Architecture:** Split `games.status` (cache truth) from a new `last_job_outcome` column (how the last job ended), funnel every truth write through a single `record_measurement()` function, replace the sweep's `ORDER BY id` with least-recently-attempted ordering that has no status filter at all, and gate Epic downloads on positive measured evidence.

**Tech Stack:** Python 3.12, SQLite (STRICT tables), pytest, structlog, FastAPI.

**Spec:** `docs/superpowers/specs/2026-09-04-cache-validation-integrity-design.md`

## Global Constraints

- Run tests as `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest`. The PATH prefix is required or `tests/test_licenses.py` false-fails. Bare `python` is not on PATH.
- **The existing status vocabulary is fixed and must not change:** `unknown`, `not_downloaded`, `up_to_date`, `pending_update`, `downloading`, `validation_failed`, `blocked`, `failed`. `games.status` has a CHECK constraint; SQLite cannot alter one in place.
- Migrations are append-only. Every new `.sql` file needs a matching `CHECKSUMS` line: `NNNN  <sha256 of file bytes>  <filename>`.
- All new tables/columns follow the existing STRICT-table conventions.
- Structured logging on every significant operation, via `structlog`.
- Per the project build loop, mark each step with `scripts/process-checklist.sh`.

---

### Task 1: Migration 0015 — new columns, repair, backfill

**Files:**
- Create: `src/orchestrator/db/migrations/0015_games_measurement_split.sql`
- Modify: `src/orchestrator/db/migrations/CHECKSUMS` (append one line)
- Test: `tests/db/test_migration_0015_measurement_split.py`

**Interfaces:**
- Consumes: nothing
- Produces: columns `games.status_measured_at TEXT`, `games.last_measure_attempt_at TEXT`, `games.last_job_outcome TEXT`, `games.last_job_outcome_at TEXT`; index `idx_games_measure_attempt`

**Why no table rebuild:** these are pure `ADD COLUMN`s. `last_error` is retained rather than renamed, because renaming it would require the snapshot/drop/recreate recipe and break existing readers. `last_job_outcome` is the new canonical field; `last_error` is left in place, unread by new code, and removed in a later cleanup.

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_migration_0015_measurement_split.py
import sqlite3
from orchestrator.db.migrate import apply_all

def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

def test_0015_adds_measurement_columns(migrated_conn):
    cols = _cols(migrated_conn, "games")
    assert "status_measured_at" in cols
    assert "last_measure_attempt_at" in cols
    assert "last_job_outcome" in cols
    assert "last_job_outcome_at" in cols

def test_0015_repairs_only_corrupted_rows(fresh_conn):
    fresh_conn.executescript("""
        INSERT INTO platforms (name) VALUES ('epic');
        INSERT INTO games (id, platform, app_id, title, status, last_validated_at) VALUES
          (1, 'epic', 'a', 'Damaged partial', 'validation_failed', '2026-09-01 03:10:00'),
          (2, 'epic', 'b', 'Damaged failed',  'failed',            '2026-09-01 03:44:50'),
          (3, 'epic', 'c', 'Good in window',  'up_to_date',        '2026-09-01 03:30:09'),
          (4, 'epic', 'd', 'Old not_dl',      'not_downloaded',    '2026-06-18 23:46:33'),
          (5, 'epic', 'e', 'Outside window',  'validation_failed', '2026-08-30 12:00:00');
    """)
    apply_all(fresh_conn)
    rows = dict(fresh_conn.execute("SELECT id, status FROM games"))
    assert rows[1] == "unknown"          # damaged, reset
    assert rows[2] == "unknown"          # damaged, reset
    assert rows[3] == "up_to_date"       # good measurement in window, untouched
    assert rows[4] == "not_downloaded"   # genuine stale measurement, untouched
    assert rows[5] == "validation_failed"  # outside the window, untouched

def test_0015_backfills_attempt_from_last_validated(fresh_conn):
    fresh_conn.executescript("""
        INSERT INTO platforms (name) VALUES ('steam');
        INSERT INTO games (id, platform, app_id, title, status, last_validated_at)
        VALUES (1, 'steam', '1', 'Old', 'not_downloaded', '2026-06-18 23:46:33');
    """)
    apply_all(fresh_conn)
    row = fresh_conn.execute(
        "SELECT last_measure_attempt_at FROM games WHERE id=1"
    ).fetchone()
    assert row[0] == '2026-06-18 23:46:33'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/db/test_migration_0015_measurement_split.py -v`
Expected: FAIL — the migration file does not exist, so columns are absent.

- [ ] **Step 3: Write the migration**

```sql
-- 0015_games_measurement_split.sql
-- Separate cache truth from job outcome (design 2026-09-04).
--
-- games.status previously carried two unrelated meanings: what a measurement
-- found on disk, and how the last job ended. On 2026-09-01 03:00-03:47 an
-- interrupted prefill batch stamped 1769 games with the dead job's outcome, and
-- Epic's scheduled prefill (which selects on status) would have re-downloaded
-- 655 already-cached titles.
--
-- Pure ADD COLUMNs: the status CHECK constraint and its vocabulary are unchanged,
-- so no table rebuild is needed.

ALTER TABLE games ADD COLUMN status_measured_at      TEXT;
ALTER TABLE games ADD COLUMN last_measure_attempt_at TEXT;
ALTER TABLE games ADD COLUMN last_job_outcome        TEXT;
ALTER TABLE games ADD COLUMN last_job_outcome_at     TEXT;

-- Repair. Reset ONLY rows corrupted in the incident window. Legitimately-cached
-- rows also fall inside it (Epic up_to_date runs from 03:30:09), so the predicate
-- excludes them: resetting a good measurement discards information for no gain.
UPDATE games
   SET status = 'unknown',
       status_measured_at = NULL
 WHERE status IN ('validation_failed', 'failed')
   AND last_validated_at >= '2026-09-01 03:00:00'
   AND last_validated_at <= '2026-09-01 03:47:59';

-- Seed the measurement queue ordering. NULL would tie the whole library at the
-- front and fall back to arbitrary id order -- the exact bias being removed.
-- Seeding from last_validated_at puts the June-stamped rows ahead of September's.
UPDATE games SET last_measure_attempt_at = last_validated_at;

-- Truth rows that survived the repair keep their measurement timestamp.
UPDATE games SET status_measured_at = last_validated_at
 WHERE status IN ('up_to_date', 'validation_failed', 'not_downloaded')
   AND last_validated_at IS NOT NULL;

CREATE INDEX idx_games_measure_attempt
    ON games(last_measure_attempt_at);

-- Durable transition log for the circuit breaker (Task 3). An in-memory counter
-- would reset on restart -- and a restart is precisely the scenario that produced
-- the 2026-09-01 corruption, so the count must survive one.
CREATE TABLE measurement_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    prior       TEXT NOT NULL,
    new_status  TEXT NOT NULL,
    downward    INTEGER NOT NULL CHECK (downward IN (0, 1)),
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE INDEX idx_measurement_transitions_window
    ON measurement_transitions(occurred_at DESC) WHERE downward = 1;
```

- [ ] **Step 4: Append the checksum**

```bash
cd "/Users/karl/Documents/Claude Projects/lancache_orchestrator"
F=src/orchestrator/db/migrations/0015_games_measurement_split.sql
printf '0015  %s  %s\n' "$(shasum -a 256 "$F" | cut -d' ' -f1)" "$(basename "$F")" \
  >> src/orchestrator/db/migrations/CHECKSUMS
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/db/ -v`
Expected: PASS, including the existing `test_migrate.py` checksum verification.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/db/migrations/0015_games_measurement_split.sql \
        src/orchestrator/db/migrations/CHECKSUMS \
        tests/db/test_migration_0015_measurement_split.py
git commit -m "feat(db): migration 0015 splits cache truth from job outcome"
```

---

### Task 2: `record_measurement()` — the single truth writer

**Files:**
- Create: `src/orchestrator/jobs/measurement.py`
- Modify: `src/orchestrator/jobs/handlers/validate.py:66-85`
- Test: `tests/jobs/test_measurement.py`

**Interfaces:**
- Consumes: Task 1's columns
- Produces:
  - `async def record_measurement(pool: Pool, game_id: int, outcome: str) -> None` — outcome is one of `"cached"`, `"partial"`, `"missing"`, `"error"`. Writes `status` + `status_measured_at` + `last_measure_attempt_at` for the first three; for `"error"` writes **only** `last_measure_attempt_at`.
  - `async def record_job_outcome(pool: Pool, game_id: int, outcome: str) -> None` — writes `last_job_outcome` + `last_job_outcome_at`. Never touches truth columns.
  - `_STATUS_FOR: dict[str, str]` moves here from `validate.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/test_measurement.py
import pytest
from orchestrator.jobs.measurement import record_measurement, record_job_outcome

@pytest.mark.asyncio
async def test_error_outcome_never_writes_truth(pool, game_up_to_date):
    await record_measurement(pool, game_up_to_date, "error")
    row = await pool.read_one(
        "SELECT status, status_measured_at, last_measure_attempt_at FROM games WHERE id=?",
        (game_up_to_date,),
    )
    assert row["status"] == "up_to_date"        # truth untouched
    assert row["status_measured_at"] is None    # not stamped as measured
    assert row["last_measure_attempt_at"] is not None  # attempt WAS recorded

@pytest.mark.asyncio
async def test_success_writes_truth_and_attempt(pool, game_unknown):
    await record_measurement(pool, game_unknown, "cached")
    row = await pool.read_one(
        "SELECT status, status_measured_at, last_measure_attempt_at FROM games WHERE id=?",
        (game_unknown,),
    )
    assert row["status"] == "up_to_date"
    assert row["status_measured_at"] is not None
    assert row["last_measure_attempt_at"] is not None

@pytest.mark.asyncio
async def test_job_outcome_never_touches_status(pool, game_up_to_date):
    await record_job_outcome(pool, game_up_to_date, "prefill interrupted")
    row = await pool.read_one(
        "SELECT status, last_job_outcome FROM games WHERE id=?", (game_up_to_date,)
    )
    assert row["status"] == "up_to_date"
    assert row["last_job_outcome"] == "prefill interrupted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/jobs/test_measurement.py -v`
Expected: FAIL with `ModuleNotFoundError: orchestrator.jobs.measurement`

- [ ] **Step 3: Write the implementation**

```python
# src/orchestrator/jobs/measurement.py
"""The ONLY module permitted to write games.status / games.status_measured_at.

Enforced by tests/test_measurement_writer_guard.py, which fails the build if any
other module issues an UPDATE against those columns. See the 2026-09-04 design:
job outcomes must never be recorded as cache-content findings.
"""
from __future__ import annotations

import structlog

from orchestrator.db.pool import Pool

_log = structlog.get_logger(__name__)

# outcome -> games.status. 'error' is absent by design: an infrastructure failure
# is not a measurement and must never overwrite cache truth.
_STATUS_FOR = {
    "cached": "up_to_date",
    "partial": "validation_failed",
    "missing": "not_downloaded",
}


async def record_measurement(pool: Pool, game_id: int, outcome: str) -> None:
    """Record the result of a cache measurement.

    A real result writes cache truth and both timestamps. An 'error' outcome
    writes ONLY last_measure_attempt_at, so the game rotates to the back of the
    measurement queue without its status being touched.
    """
    new_status = _STATUS_FOR.get(outcome)
    if new_status is None:
        await pool.execute_write(
            "UPDATE games SET last_measure_attempt_at=CURRENT_TIMESTAMP WHERE id=?",
            (game_id,),
        )
        _log.info("measurement.attempt_only", game_id=game_id, outcome=outcome)
        return

    await pool.execute_write(
        "UPDATE games SET status=?, status_measured_at=CURRENT_TIMESTAMP, "
        "last_measure_attempt_at=CURRENT_TIMESTAMP, last_validated_at=CURRENT_TIMESTAMP "
        "WHERE id=?",
        (new_status, game_id),
    )
    _log.info("measurement.recorded", game_id=game_id, status=new_status)


async def record_job_outcome(pool: Pool, game_id: int, outcome: str) -> None:
    """Record how a job ended. Never touches cache truth."""
    await pool.execute_write(
        "UPDATE games SET last_job_outcome=?, last_job_outcome_at=CURRENT_TIMESTAMP "
        "WHERE id=?",
        (outcome[:200], game_id),
    )
```

- [ ] **Step 4: Rewire `validate.py` to delegate**

Replace `validate.py:66-85` (the `new_status` block and its `else` branch) with:

```python
    await record_measurement(pool, game_id, result.outcome)
```

Delete the local `_STATUS_FOR` at `validate.py:33-38` and import from the new module. Note the behaviour change: the old `else` branch wrote `status='failed'` for games stuck in `'downloading'`; that write is dropped, because it is a job outcome, not a measurement. The startup job reaper (ID6) already resolves stranded `downloading` rows.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/jobs/ tests/validator/ -v`
Expected: PASS. Existing validate tests asserting `status='failed'` on the downloading path must be updated to assert the status is unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator/jobs/measurement.py \
        src/orchestrator/jobs/handlers/validate.py \
        tests/jobs/test_measurement.py
git commit -m "feat(jobs): single record_measurement writer for cache truth"
```

---

### Task 3: Circuit breaker — 25 downward transitions per 60 minutes

**Files:**
- Modify: `src/orchestrator/jobs/measurement.py`
- Modify: `src/orchestrator/core/settings.py` (two new settings)
- Test: `tests/jobs/test_measurement_circuit_breaker.py`

**Interfaces:**
- Consumes: `record_measurement()` from Task 2
- Produces:
  - `_RANK: dict[str, int]` — `{"up_to_date": 3, "validation_failed": 2, "not_downloaded": 1, "unknown": 0}`
  - `class CircuitBreakerTripped(RuntimeError)`
  - settings `measurement_breaker_threshold: int = 25`, `measurement_breaker_window_minutes: int = 60`

**Why this matters beyond job corruption:** a degraded agent returns *plausible but false* measurements — the 2026-08-31 incident had it reading 65 of 256 cache buckets and reporting ~8% cached on every game. Those pass every validity check. A mass-downward-transition alarm is the only thing that catches them.

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/test_measurement_circuit_breaker.py
import pytest
from orchestrator.jobs.measurement import record_measurement, CircuitBreakerTripped

@pytest.mark.asyncio
async def test_trips_after_25_downward_transitions(pool, make_games):
    ids = await make_games(30, status="up_to_date")
    with pytest.raises(CircuitBreakerTripped):
        for gid in ids:
            await record_measurement(pool, gid, "missing")  # up_to_date -> not_downloaded

@pytest.mark.asyncio
async def test_upward_transitions_never_trip(pool, make_games):
    ids = await make_games(200, status="unknown")
    for gid in ids:
        await record_measurement(pool, gid, "cached")  # unknown -> up_to_date, rank 0 -> 3
    row = await pool.read_one("SELECT COUNT(*) AS n FROM games WHERE status='up_to_date'")
    assert row["n"] == 200

@pytest.mark.asyncio
async def test_error_outcomes_never_count(pool, make_games):
    ids = await make_games(100, status="up_to_date")
    for gid in ids:
        await record_measurement(pool, gid, "error")  # no truth write at all
    row = await pool.read_one("SELECT COUNT(*) AS n FROM games WHERE status='up_to_date'")
    assert row["n"] == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/jobs/test_measurement_circuit_breaker.py -v`
Expected: FAIL with `ImportError: cannot import name 'CircuitBreakerTripped'`

- [ ] **Step 3: Write the implementation**

Add to `src/orchestrator/jobs/measurement.py`:

```python
_RANK = {"up_to_date": 3, "validation_failed": 2, "not_downloaded": 1, "unknown": 0}


class CircuitBreakerTripped(RuntimeError):
    """Too many games lost cache state too fast. Writing has stopped."""


def _is_downward(prior: str | None, new: str) -> bool:
    """True only for a drop between two ranked truth states.

    pending_update / downloading / blocked / failed carry no rank -- they are not
    cache truth, so transitions involving them are ignored entirely.
    """
    if prior is None:
        return False
    p, n = _RANK.get(prior), _RANK.get(new)
    if p is None or n is None:
        return False
    return n < p
```

Rewrite the truth-writing branch of `record_measurement()` as:

```python
    prior_row = await pool.read_one("SELECT status FROM games WHERE id=?", (game_id,))
    prior = prior_row["status"] if prior_row else None
    downward = _is_downward(prior, new_status)

    if downward:
        s = get_settings()
        recent = await pool.read_one(
            "SELECT COUNT(*) AS n FROM measurement_transitions "
            "WHERE downward = 1 "
            "  AND occurred_at >= datetime('now', ?)",
            (f"-{s.measurement_breaker_window_minutes} minutes",),
        )
        if recent and recent["n"] + 1 >= s.measurement_breaker_threshold:
            _log.error(
                "measurement.breaker_tripped",
                game_id=game_id, prior=prior, new_status=new_status,
                downward_in_window=recent["n"] + 1,
                threshold=s.measurement_breaker_threshold,
            )
            await _notify_breaker(recent["n"] + 1)
            raise CircuitBreakerTripped(
                f"{recent['n'] + 1} games lost cache state within "
                f"{s.measurement_breaker_window_minutes} minutes; writing halted"
            )

    await pool.execute_write(
        "INSERT INTO measurement_transitions (game_id, prior, new_status, downward) "
        "VALUES (?, ?, ?, ?)",
        (game_id, prior or "unknown", new_status, 1 if downward else 0),
    )
```

**Notification.** Do **not** build an SMTP path. The container already carries
Uptime Kuma push URLs (`ORCH_KUMA_PUSH_SCHEDULED_PREFILL=http://10.100.23.57:3001/api/push/...`),
and Kuma owns notification delivery. Add `ORCH_KUMA_PUSH_MEASUREMENT_BREAKER` and:

```python
async def _notify_breaker(count: int) -> None:
    """Best-effort push to Uptime Kuma. Never raises -- a dead monitor must not
    suppress the exception that actually halts writing."""
    url = get_settings().kuma_push_measurement_breaker
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(url, params={"status": "down",
                                          "msg": f"{count} games lost cache state"})
    except Exception as e:
        _log.warning("measurement.breaker_notify_failed", reason=str(e)[:200])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/jobs/test_measurement_circuit_breaker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/jobs/measurement.py src/orchestrator/core/settings.py \
        tests/jobs/test_measurement_circuit_breaker.py
git commit -m "feat(jobs): circuit breaker on mass cache-state loss"
```

---

### Task 4: Build-breaking source guard

**Files:**
- Test: `tests/test_measurement_writer_guard.py`

**Interfaces:**
- Consumes: `src/orchestrator/jobs/measurement.py` from Task 2
- Produces: nothing importable — this task exists purely to fail the build on regression

- [ ] **Step 1: Write the test (it must pass immediately once Tasks 2-3 land)**

```python
# tests/test_measurement_writer_guard.py
"""Fails the build if any module other than jobs/measurement.py writes cache truth.

This is the structural guarantee behind the 2026-09-04 design: a job outcome can
never again be recorded as a cache-content finding, because only one function is
permitted to write games.status.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
ALLOWED = {SRC / "jobs" / "measurement.py"}
PATTERN = re.compile(r"UPDATE\s+games\s+SET[^\"']*\b(status|status_measured_at)\s*=", re.I | re.S)

def test_only_measurement_module_writes_cache_truth():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in ALLOWED:
            continue
        if PATTERN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, (
        "These modules write games.status directly. Route them through "
        "orchestrator.jobs.measurement.record_measurement() instead: " + ", ".join(offenders)
    )
```

- [ ] **Step 2: Run it and fix every offender it names**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_measurement_writer_guard.py -v`
Expected: FAIL initially, listing modules such as the prefill and purge handlers. Rewrite each to call `record_measurement()` or `record_job_outcome()`. Re-run until PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_measurement_writer_guard.py src/orchestrator/
git commit -m "test: fail the build on out-of-band cache-truth writes"
```

---

### Task 5: Sweep — least-recently-attempted ordering, no status filter

**Files:**
- Modify: `src/orchestrator/jobs/handlers/sweep.py:30-39,65-66`
- Test: `tests/jobs/handlers/test_sweep_ordering.py`

**Interfaces:**
- Consumes: `last_measure_attempt_at` from Task 1
- Produces: `_CANDIDATE_SQL` and `_CANDIDATE_SQL_FULL` with new definitions

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/handlers/test_sweep_ordering.py
import pytest

@pytest.mark.asyncio
async def test_not_downloaded_games_are_candidates(pool, seed_games, run_sweep):
    """The D2 regression: 1357 Steam games were invisible from 2026-06-18."""
    gid = await seed_games(status="not_downloaded", owned=1)
    seen = await run_sweep(pool)
    assert gid in seen

@pytest.mark.asyncio
async def test_oldest_attempt_goes_first(pool, seed_games, candidate_ids):
    new = await seed_games(status="up_to_date", last_measure_attempt_at="2026-09-01 00:00:00")
    old = await seed_games(status="up_to_date", last_measure_attempt_at="2026-06-01 00:00:00")
    never = await seed_games(status="unknown", last_measure_attempt_at=None)
    assert await candidate_ids(pool) == [never, old, new]

@pytest.mark.asyncio
async def test_failed_attempt_rotates_to_back(pool, seed_games, candidate_ids):
    """Head-of-line blocking: a game that always times out must not pin the queue."""
    stuck = await seed_games(status="unknown", last_measure_attempt_at=None)
    other = await seed_games(status="unknown", last_measure_attempt_at="2026-06-01 00:00:00")
    from orchestrator.jobs.measurement import record_measurement
    await record_measurement(pool, stuck, "error")
    assert (await candidate_ids(pool))[0] == other
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/jobs/handlers/test_sweep_ordering.py -v`
Expected: FAIL — `not_downloaded` is excluded by the current filter, and ordering is by `id`.

- [ ] **Step 3: Write the implementation**

Replace `sweep.py:30-39` with:

```python
# Gated sweep: every owned game, least-recently-attempted first.
#
# There is deliberately NO `status IN (...)` filter. The previous filter omitted
# 'not_downloaded', which made 1357 Steam games invisible to every sweep from
# 2026-06-18 onward. Selecting on status at all is the bug class; removing the
# filter removes it permanently rather than adding one more value to a list.
#
# Ordering by last_measure_attempt_at (NULLS FIRST) replaces the old ORDER BY id.
# It needs no persisted cursor: games already attempted sort to the back, so an
# interrupted sweep resumes correctly on its next run by construction.
_CANDIDATE_SQL = (
    "SELECT id, status FROM games "
    "WHERE owned = 1 "
    "ORDER BY last_measure_attempt_at ASC NULLS FIRST, id ASC"
)

# `full` mode additionally includes unowned games.
_CANDIDATE_SQL_FULL = (
    "SELECT id, status FROM games "
    "ORDER BY last_measure_attempt_at ASC NULLS FIRST, id ASC"
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/jobs/handlers/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/jobs/handlers/sweep.py tests/jobs/handlers/test_sweep_ordering.py
git commit -m "fix(sweep): order by least-recently-attempted, drop the status filter"
```

---

### Task 6: Timeouts write no cache truth

**Files:**
- Modify: `src/orchestrator/jobs/handlers/sweep.py:76-100`
- Modify: `src/orchestrator/core/settings.py` (per-game timeout scaling)
- Test: `tests/jobs/handlers/test_sweep_timeout_safety.py`

**Interfaces:**
- Consumes: `record_measurement()` from Task 2
- Produces: `validate_timeout_for(chunks_total: int) -> float`

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/handlers/test_sweep_timeout_safety.py
import pytest

@pytest.mark.asyncio
async def test_cancelled_sweep_writes_no_cache_truth(pool, seed_games, run_sweep_cancelled):
    """Replay of 2026-09-01: a cancelled sweep must corrupt nothing."""
    gid = await seed_games(status="up_to_date")
    await run_sweep_cancelled(pool)
    row = await pool.read_one("SELECT status, last_measure_attempt_at FROM games WHERE id=?", (gid,))
    assert row["status"] == "up_to_date"
    assert row["last_measure_attempt_at"] is not None  # attempt still recorded

def test_timeout_scales_with_chunk_count():
    from orchestrator.core.settings import validate_timeout_for
    # ARK ModKit measured at ~433s against a fixed 300s limit -- it could never pass.
    assert validate_timeout_for(45415) > 433
    assert validate_timeout_for(100) < validate_timeout_for(45415)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/jobs/handlers/test_sweep_timeout_safety.py -v`
Expected: FAIL — `validate_timeout_for` does not exist.

- [ ] **Step 3: Write the implementation**

In `settings.py`, add the three settings the function needs (the base covers a
small title's fixed overhead; the per-chunk term is derived from ARK ModKit's
measured ~433s over 45,415 chunks, rounded up with headroom):

```python
    validate_timeout_base_seconds: float = Field(default=60.0, gt=0)
    validate_timeout_per_chunk_seconds: float = Field(default=0.012, gt=0)
    validate_timeout_max_seconds: float = Field(default=1800.0, gt=0)
```

Then:

```python
def validate_timeout_for(chunks_total: int) -> float:
    """Per-game validation timeout, scaled by chunk count.

    A fixed 300s limit made large titles permanently unvalidatable: ARK ModKit
    measured ~433s and could never finish inside it.
    """
    s = get_settings()
    return min(
        s.validate_timeout_max_seconds,
        s.validate_timeout_base_seconds + chunks_total * s.validate_timeout_per_chunk_seconds,
    )
```

In `sweep.py`, replace the body of `_one()`'s `try` block with:

```python
            try:
                async with asyncio.timeout(validate_timeout_for(chunks_total)):
                    result = await validate_one_game(deps.pool, deps, game_id, settings)
            except asyncio.CancelledError:
                # The 6h budget expired, or the job was cancelled. Record the
                # ATTEMPT so the game rotates to the back, write no cache truth,
                # then re-raise so the job actually ends. This is the 2026-09-01
                # replay: a cancelled sweep must corrupt nothing.
                await record_measurement(deps.pool, game_id, "error")
                raise
            except (TimeoutError, Exception) as e:
                await record_measurement(deps.pool, game_id, "error")
                async with lock:
                    errors += 1
                _log.warning(
                    "sweep.game_error", job_id=job_id, game_id=game_id,
                    error=type(e).__name__, reason=str(e)[:200],
                )
                return
```

`chunks_total` comes from the game's most recent `validation_history` row; when a
game has never been measured, fall back to `validate_timeout_max_seconds` rather
than the base, so a first-ever measurement of a large title is not guaranteed to
time out.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/jobs/handlers/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/jobs/handlers/sweep.py src/orchestrator/core/settings.py \
        tests/jobs/handlers/test_sweep_timeout_safety.py
git commit -m "fix(sweep): a timeout records an attempt, never cache truth"
```

---

### Task 7: Evidence-based Epic download policy

**Files:**
- Modify: `src/orchestrator/scheduler/jobs.py:165-176`
- Test: `tests/scheduler/test_scheduled_prefill_evidence.py`

**Interfaces:**
- Consumes: `status_measured_at` from Task 1
- Produces: no new symbols — behaviour change only

- [ ] **Step 1: Write the failing test**

```python
# tests/scheduler/test_scheduled_prefill_evidence.py
import pytest
from orchestrator.scheduler.jobs import enqueue_scheduled_prefill

@pytest.mark.asyncio
async def test_unknown_games_are_never_queued(pool, seed_games):
    """The 655-download bug: 'not proven cached' must not mean 'download it'."""
    await seed_games(platform="epic", status="unknown", status_measured_at=None, owned=1)
    assert await enqueue_scheduled_prefill(pool) == 0

@pytest.mark.asyncio
async def test_measured_missing_game_is_queued(pool, seed_games):
    await seed_games(platform="epic", status="not_downloaded",
                     status_measured_at="2026-09-04 12:00:00", owned=1)
    assert await enqueue_scheduled_prefill(pool) == 1

@pytest.mark.asyncio
async def test_unmeasured_missing_game_is_not_queued(pool, seed_games):
    """Status says missing but nothing ever measured it -- no evidence, no download."""
    await seed_games(platform="epic", status="not_downloaded",
                     status_measured_at=None, owned=1)
    assert await enqueue_scheduled_prefill(pool) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/scheduler/test_scheduled_prefill_evidence.py -v`
Expected: FAIL — the current `status <> 'up_to_date'` predicate queues unknown games.

- [ ] **Step 3: Write the implementation**

In `scheduler/jobs.py`, replace the line `"  AND g.status <> 'up_to_date' "` with:

```python
            # Evidence-based (2026-09-04). Previously `status <> 'up_to_date'`,
            # which meant "not proven cached" == "download it" -- so 1769 games
            # corrupted by an interrupted prefill batch would have queued 655
            # Epic downloads for titles already on disk. A download now requires a
            # real measurement that actually found the game absent or incomplete.
            "  AND g.status IN ('validation_failed', 'not_downloaded') "
            "  AND g.status_measured_at IS NOT NULL "
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/scheduler/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/scheduler/jobs.py tests/scheduler/test_scheduled_prefill_evidence.py
git commit -m "fix(scheduler): Epic prefill requires measured evidence of absence"
```

---

### Task 8: Full suite, documentation, PR

**Files:**
- Modify: `CHANGELOG.md`, `FEATURES.md`, `PROJECT_BIBLE.md`, `CLAUDE.md` (Current State)

- [ ] **Step 1: Run the entire suite**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest`
Expected: PASS. Baseline before this work was 1636 passing.

- [ ] **Step 2: Update the docs**

`CHANGELOG.md` under `[Unreleased]` — Data Model (migration 0015), Fixed (sweep ordering, `not_downloaded` dead end, timeout corruption, Epic download policy), Security/Infrastructure (circuit breaker + source guard).

`PROJECT_BIBLE.md` — record that `games.status` is now written by exactly one function.

`CLAUDE.md` Current State — note the new columns and the measurement-before-download rule.

- [ ] **Step 3: Commit and open the PR**

```bash
git add CHANGELOG.md FEATURES.md PROJECT_BIBLE.md CLAUDE.md
git commit -m "docs: record the cache validation integrity work"
git push -u origin docs/cache-validation-integrity-design
gh pr create --base main \
  --title "fix(cache): separate cache truth from job outcome" \
  --body "$(cat <<'PRBODY'
Implements docs/superpowers/specs/2026-09-04-cache-validation-integrity-design.md

Three defects observed live on 2026-09-01:

1. The sweep could never finish. `ORDER BY id` with no resume meant it burned its
   6h budget on low ids; job 46446 moved steam up_to_date 6 -> 46 out of 3197
   candidates and was cancelled. Games past that point were unreachable.
2. `not_downloaded` was absent from the candidate filter, so 1357 Steam games
   stamped 2026-06-18 were invisible to every sweep since.
3. Job outcomes were written into `games.status`, which is also Epic's download
   trigger. An interrupted prefill batch stamped 1769 games and would have
   re-downloaded 655 already-cached titles.

Changes: migration 0015 adds the truth/outcome column split plus a durable
transition log; `record_measurement()` becomes the only writer of cache truth,
enforced by a source-scanning test that fails the build; the sweep orders by
least-recently-attempted with no status filter at all; timeouts record an attempt
and never truth; Epic prefill requires measured evidence of absence.

Migration 0015 resets only rows corrupted inside the incident window -- good
measurements inside it are preserved.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
PRBODY
)"
```

- [ ] **Step 4: Hand the PR to the Orchestrator to merge**

Do not merge. Karl merges.

---

## Post-merge operational sequence

1. Deploy to LXC 1105 with a `dpa-pre-N` rollback tag on both hosts first.
2. Let the sweep run. It now reaches every owned game, oldest-attempt first, and survives interruption. Expect days.
3. Watch for `measurement.breaker_tripped`. If it fires during the recovery, something is genuinely wrong — investigate before clearing it.
4. Once measurement has converged, re-enable Epic prefill by setting `ORCH_SCHEDULED_PREFILL_ENABLED=true` in `/root/orch-lxc.env`. It will then queue only games measurement proved absent — the real number, not 655.

## Deferred to Game_shelf (separate repo)

- Remove `Failed` from the cache-status filter — after the orchestrator deploy.
- Relabel `Partial` to `Partly cached` in `frontend/src/utils/cacheBadge.js` (`partialLabel()`) plus `cacheBadge.test.js` assertions.
- Humble Bundle missing from filter options — independent; GS #22 remediation already open.
