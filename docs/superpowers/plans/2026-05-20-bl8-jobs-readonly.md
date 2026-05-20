# BL8 Jobs Read-Only Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `GET /api/v1/jobs` — second paginated F9 read endpoint — composing the BL7+UAT-4 conventions with zero changes to `_query_helpers.py`. Validates that future paginated F9 endpoints can be added cheaply.

**Architecture:** New router at `src/orchestrator/api/routers/jobs.py` (~190 LoC: per-endpoint allow-lists + Pydantic response models + handler that composes `parse_pagination`/`parse_filters`/`parse_sort`/`build_where_clause`/`build_order_by_clause`). Wired into `main.py` via `app.include_router`. Two-query SQL pattern: `SELECT COUNT(*)` for `meta.total` + `SELECT ... LIMIT/OFFSET` for rows. `payload` column parsed as JSON with UAT-4 size cap + RecursionError catch. `error` truncated to 200 chars at API layer. Default sort `id:desc` (per spec D1). New `jobs_pool_seeded` fixture in `tests/api/conftest.py`. ~25 tests in `tests/api/test_jobs_router.py`.

**Tech Stack:** Python 3.12, FastAPI 0.136.1, Pydantic v2, aiosqlite, structlog, httpx (test client), pytest-asyncio.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/orchestrator/api/routers/jobs.py` | **Create** (~190 LoC) | `JobResponse`, `JobsMeta`, `JobListResponse` Pydantic models; `JOBS_FILTER_ALLOW_LIST`, `JOBS_SORT_ALLOW_LIST` constants; `list_jobs` handler |
| `src/orchestrator/api/main.py` | **Modify** (+1 import, +1 `include_router` line) | Wire the new router |
| `tests/api/conftest.py` | **Modify** (+1 fixture: `jobs_pool_seeded`) | ~50-row jobs seed for pagination/filter/sort tests |
| `tests/api/test_jobs_router.py` | **Create** (~480 LoC, ~25 tests) | HTTP-level tests via httpx ASGI transport |
| `docs/security-audits/bl8-f9-jobs-readonly-security-audit.md` | **Create** | Per-feature audit doc (Build Loop gate requirement) |
| `CHANGELOG.md` | **Modify** | BL8 entry under `[Unreleased]` → `### Added` |
| `FEATURES.md` | **Modify** | New Feature 8 entry |

No `_query_helpers.py` changes — the BL7+UAT-4 module covers every BL8 capability.

---

## Task 0: Commit this plan to git

**Files:**
- Already written: `docs/superpowers/plans/2026-05-20-bl8-jobs-readonly.md`

**Branch state:** Already on `feat/bl8-jobs-readonly`. Spec committed in `2db87b0`. `--start-feature "BL8-F9-jobs-readonly"` already marked.

- [ ] **Step 1: Confirm plan file is untracked**

Run:
```bash
git status --short docs/superpowers/plans/2026-05-20-bl8-jobs-readonly.md
```
Expected: `?? docs/superpowers/plans/2026-05-20-bl8-jobs-readonly.md`

- [ ] **Step 2: Write commit message to a tmp file**

Write `/tmp/bl8-plan-commit.txt`:
```
docs(plan): BL8 jobs read-only implementation plan

Decomposes the BL8 spec (2db87b0) into 9 tasks following the
project Build Loop:
- Task 0: this plan commit
- Task 1: write jobs_pool_seeded conftest fixture
- Task 2: write router tests (red phase)
- Task 3: implement routers/jobs.py + wire main.py (green phase)
- Task 4: mark tests_written + tests_verified_failing + implemented
- Task 5: security audit (ruff, mypy, semgrep, gitleaks) + audit doc
- Task 6: CHANGELOG + FEATURES + mark documentation_updated
- Task 7: combined feat+docs commit
- Task 8: mark feature_recorded + record in test-gate counter
- Task 9: push + open PR (do not merge per memory)

This BL validates the proposition that UAT-4-hardened
_query_helpers.py conventions propagate cheaply: zero changes to
the shared module; new endpoint is allow-list declaration +
handler composition.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

- [ ] **Step 3: Mark evaluation, stage, commit**

Run:
```bash
bash .claude/framework/hooks/mark-evaluated.sh "BL8 plan commit — BL6/BL7 docs(spec)-then-docs(plan) pattern; user pre-approved end-to-end"
git add docs/superpowers/plans/2026-05-20-bl8-jobs-readonly.md
git commit -F /tmp/bl8-plan-commit.txt
```

- [ ] **Step 4: Verify**

Run:
```bash
git log --oneline -2
git status --short
```
Expected: top commit subject `docs(plan): BL8 jobs read-only implementation plan`. Working tree clean.

---

## Task 1: Add `jobs_pool_seeded` fixture to conftest.py

**Files:**
- Modify: `tests/api/conftest.py` (+1 fixture after the existing `games_pool_100`)

- [ ] **Step 1: TaskUpdate the relevant in_progress task** (enforce-plan-tracking hook)

Use TaskUpdate tool to mark the active task in_progress before editing source/test files.

- [ ] **Step 2: Read current conftest.py to find the right insertion point**

Run:
```bash
grep -n "games_pool_100\|^@pytest_asyncio" tests/api/conftest.py
```
Expected: see the `games_pool_100` fixture and any others.

- [ ] **Step 3: Add the fixture after `games_pool_100`**

Append after the closing brace of `games_pool_100`:

```python
@pytest_asyncio.fixture
async def jobs_pool_seeded(populated_pool):  # noqa: F811
    """populated_pool seeded with ~50 jobs across kinds/states/sources/sources.

    Mix designed for BL8 filter+sort+pagination tests:
    - 5 kinds × multiple states (covers all kind/state enum values)
    - 4 sources represented
    - timestamps: queued has both NULL; running has started_at only;
      terminal states have both
    - progress: NULL for queued; partial for running; 1.0 for succeeded
    - error: populated only for failed jobs
    - payload: small dict on most; null on a few; one oversized (>64 KiB);
      one malformed JSON; one non-dict JSON (array)
    """
    import json

    async with populated_pool.write_transaction() as tx:
        # Helper to insert one job
        async def _ins(
            kind, state, *, game_id=None, platform=None, progress=None,
            source="scheduler", started_at=None, finished_at=None,
            error=None, payload=None,
        ):
            await tx.execute(
                "INSERT INTO jobs "
                "(kind, game_id, platform, state, progress, source, "
                " started_at, finished_at, error, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind, game_id, platform, state, progress, source,
                    started_at, finished_at, error, payload,
                ),
            )

        # Queued jobs (5)
        for i in range(5):
            await _ins(
                kind=["prefill", "validate", "library_sync", "auth_refresh", "sweep"][i],
                state="queued",
                game_id=(i + 1) if i < 3 else None,
                platform="steam" if i % 2 == 0 else "epic",
                payload=json.dumps({"queued_at": f"2026-05-20T1{i}:00:00Z"}),
            )

        # Running jobs (5)
        for i in range(5):
            await _ins(
                kind=["prefill", "prefill", "validate", "library_sync", "sweep"][i],
                state="running",
                game_id=(i + 1) if i < 4 else None,
                platform="steam" if i % 2 == 0 else "epic",
                progress=0.1 + i * 0.2,  # 0.1, 0.3, 0.5, 0.7, 0.9
                source=["scheduler", "scheduler", "cli", "gameshelf", "api"][i],
                started_at=f"2026-05-20T1{i}:00:00Z",
                payload=json.dumps({"depots": [100 + i, 101 + i]}),
            )

        # Succeeded jobs (20) — most-recent-first by id
        for i in range(20):
            await _ins(
                kind=["prefill", "validate", "library_sync"][i % 3],
                state="succeeded",
                game_id=((i % 5) + 1),
                platform="steam" if i % 2 == 0 else "epic",
                progress=1.0,
                source=["scheduler", "scheduler", "scheduler", "cli"][i % 4],
                started_at=f"2026-05-{15 + (i % 5):02d}T08:00:00Z",
                finished_at=f"2026-05-{15 + (i % 5):02d}T09:00:00Z",
                payload=json.dumps({"bytes": 1000000 * (i + 1)}),
            )

        # Failed jobs (10)
        for i in range(10):
            await _ins(
                kind=["prefill", "auth_refresh"][i % 2],
                state="failed",
                game_id=((i % 3) + 1),
                platform="steam",
                progress=0.5 + (i % 5) * 0.1,
                source="scheduler",
                started_at=f"2026-05-{10 + (i % 8):02d}T10:00:00Z",
                finished_at=f"2026-05-{10 + (i % 8):02d}T11:00:00Z",
                error=f"simulated failure #{i}: " + ("x" * 50),
                payload=json.dumps({"attempt": i + 1}),
            )

        # Cancelled jobs (5)
        for i in range(5):
            await _ins(
                kind="sweep",
                state="cancelled",
                source="cli",
                started_at=f"2026-05-{5 + i:02d}T12:00:00Z",
                finished_at=f"2026-05-{5 + i:02d}T12:01:00Z",
                payload=json.dumps({"reason": "operator_abort"}),
            )

        # One job with NULL payload (id will be ~46)
        await _ins(kind="sweep", state="succeeded", source="scheduler",
                   started_at="2026-04-01T00:00:00Z",
                   finished_at="2026-04-01T00:05:00Z")

        # One job with oversized payload (>64 KiB)
        big = json.dumps({"data": "x" * 70000})
        await _ins(kind="prefill", state="succeeded", game_id=1, platform="steam",
                   progress=1.0,
                   started_at="2026-04-02T00:00:00Z",
                   finished_at="2026-04-02T01:00:00Z",
                   payload=big)

        # One job with malformed JSON payload
        await _ins(kind="validate", state="failed", game_id=2, platform="steam",
                   started_at="2026-04-03T00:00:00Z",
                   finished_at="2026-04-03T00:01:00Z",
                   error="json corrupt",
                   payload="{not valid json")

        # One job with non-dict JSON payload (array) — must surface as null
        await _ins(kind="sweep", state="succeeded", source="scheduler",
                   started_at="2026-04-04T00:00:00Z",
                   finished_at="2026-04-04T00:05:00Z",
                   payload=json.dumps([1, 2, 3]))

    return populated_pool
```

- [ ] **Step 4: Verify fixture compiles (no test runs yet)**

Run:
```bash
source .venv/bin/activate && python -c "from tests.api.conftest import jobs_pool_seeded" 2>&1 | tail -3
```
Expected: no output / no errors (pytest_asyncio decorators check via collection).

Run:
```bash
source .venv/bin/activate && pytest tests/api/ --collect-only -q 2>&1 | tail -3
```
Expected: all currently-passing tests still collect (no syntax errors introduced).

---

## Task 2: Write router tests (red phase)

**Files:**
- Create: `tests/api/test_jobs_router.py`

Tests reference the not-yet-existing router; running them fails with `ImportError` / 404 / etc. That collective failure is the red-phase signal.

- [ ] **Step 1: Write the full test file**

Create `tests/api/test_jobs_router.py`:

```python
"""Tests for GET /api/v1/jobs (BL8 / Feature 9 partial).

Covers spec §5 — empty DB, happy path, pagination, filtering, sorting,
applied-echo, error paths, auth, pool-failure, payload, error truncation.
"""

from __future__ import annotations

import json

VALID_TOKEN = "a" * 32


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


class TestJobsEmptyDb:
    async def test_empty_db_returns_empty_array(self, client, populated_pool):
        # populated_pool has no jobs by default
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["jobs"] == []
        assert body["meta"]["total"] == 0
        assert body["meta"]["has_more"] is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestJobsHappyPath:
    async def test_returns_jobs(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "jobs" in body
        assert "meta" in body
        assert len(body["jobs"]) > 0
        assert body["meta"]["total"] >= 49

    async def test_envelope_shape(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert set(body.keys()) == {"jobs", "meta"}
        assert set(body["meta"].keys()) == {
            "total", "limit", "offset", "has_more",
            "applied_filters", "applied_sort",
        }

    async def test_per_job_field_set(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        expected_fields = {
            "id", "kind", "game_id", "platform", "state", "progress",
            "source", "started_at", "finished_at", "error", "payload",
        }
        for job in body["jobs"]:
            assert set(job.keys()) == expected_fields


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestJobsPagination:
    async def test_default_limit_50(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["meta"]["limit"] == 50
        assert body["meta"]["offset"] == 0

    async def test_explicit_limit(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["jobs"]) == 10
        assert body["meta"]["limit"] == 10

    async def test_offset_progression(self, client, jobs_pool_seeded):
        r1 = await client.get(
            "/api/v1/jobs?limit=10&offset=0",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.get(
            "/api/v1/jobs?limit=10&offset=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids1 = [j["id"] for j in r1.json()["jobs"]]
        ids2 = [j["id"] for j in r2.json()["jobs"]]
        assert set(ids1).isdisjoint(set(ids2))

    async def test_limit_above_max_returns_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=1000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Filter: enum-valued fields (kind, state, platform, source)
# ---------------------------------------------------------------------------


class TestJobsFilterEnums:
    async def test_kind_eq(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?kind=prefill&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["kind"] == "prefill"

    async def test_kind_in(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?kind_in=prefill,validate&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        kinds = {j["kind"] for j in r.json()["jobs"]}
        assert kinds.issubset({"prefill", "validate"})

    async def test_state_eq_running(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?state=running&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["state"] == "running"

    async def test_state_in_active(self, client, jobs_pool_seeded):
        # Game_shelf "active jobs" canonical query
        r = await client.get(
            "/api/v1/jobs?state_in=queued,running&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        states = {j["state"] for j in r.json()["jobs"]}
        assert states.issubset({"queued", "running"})

    async def test_platform_eq(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?platform=steam&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["platform"] == "steam"

    async def test_source_eq(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?source=cli&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["source"] == "cli"


# ---------------------------------------------------------------------------
# Filter: game_id (FK) + numeric ranges
# ---------------------------------------------------------------------------


class TestJobsFilterScalars:
    async def test_game_id_eq(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?game_id=1&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["game_id"] == 1

    async def test_progress_gte(self, client, jobs_pool_seeded):
        # "Almost done" — UAT operator query
        r = await client.get(
            "/api/v1/jobs?progress_gte=0.9&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["progress"] is not None
            assert job["progress"] >= 0.9

    async def test_progress_range(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?progress_gte=0.3&progress_lte=0.7&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert 0.3 <= job["progress"] <= 0.7


# ---------------------------------------------------------------------------
# Filter: timestamp ranges (typed-string validator)
# ---------------------------------------------------------------------------


class TestJobsFilterTimeRange:
    async def test_started_at_gte(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?started_at_gte=2026-05-15&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["started_at"] is not None
            assert job["started_at"] >= "2026-05-15"

    async def test_finished_at_lte(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?finished_at_lte=2026-05-15T00:00:00Z&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            assert job["finished_at"] is not None
            assert job["finished_at"] <= "2026-05-15T00:00:00Z"

    async def test_invalid_timestamp_format_returns_400(self, client, jobs_pool_seeded):
        # UAT-4 S3-a regression: XSS-payload as timestamp must be rejected
        r = await client.get(
            "/api/v1/jobs?started_at_gte=<script>alert(1)</script>",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "timestamp" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Sort (spec D1: id:desc default; tie-breaker dedup)
# ---------------------------------------------------------------------------


class TestJobsSort:
    async def test_default_id_desc(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids = [j["id"] for j in r.json()["jobs"]]
        assert ids == sorted(ids, reverse=True)

    async def test_default_applied_sort_dedup_no_tie_breaker(self, client, jobs_pool_seeded):
        # Spec D1: default sort id:desc means user explicit id wins;
        # tie-breaker id:asc is deduped (UAT-4 S2-B behavior).
        r = await client.get(
            "/api/v1/jobs?limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied == [{"field": "id", "direction": "desc"}]

    async def test_non_id_sort_appends_tie_breaker(self, client, jobs_pool_seeded):
        # User sorts by state; server appends id:asc tie-breaker
        r = await client.get(
            "/api/v1/jobs?sort=state:asc&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied[-1] == {"field": "id", "direction": "asc"}

    async def test_started_at_desc(self, client, jobs_pool_seeded):
        # NULL handling: SQLite default puts NULLs LAST in DESC
        r = await client.get(
            "/api/v1/jobs?sort=started_at:desc&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        starts = [j["started_at"] for j in r.json()["jobs"]]
        # Filter out NULLs for ordering check
        non_null = [s for s in starts if s is not None]
        assert non_null == sorted(non_null, reverse=True)


# ---------------------------------------------------------------------------
# Applied echo (UAT-4 S2-A regression: compact plain-dict shape)
# ---------------------------------------------------------------------------


class TestJobsAppliedEcho:
    async def test_applied_filters_compact_dict_shape(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?state=running&kind=prefill&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        # Compact: only the applied op key per field; no null sibling keys
        assert applied == {
            "state": {"eq": "running"},
            "kind": {"eq": "prefill"},
        }
        # Explicit no-null check
        for ops in applied.values():
            assert None not in ops.values()

    async def test_applied_filters_in_op_alias(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?state_in=queued,running",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert "in" in applied["state"]
        assert applied["state"]["in"] == ["queued", "running"]


# ---------------------------------------------------------------------------
# Payload + error
# ---------------------------------------------------------------------------


class TestJobsPayloadAndError:
    async def test_well_formed_payload_parsed(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?state=running&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for job in r.json()["jobs"]:
            if job["payload"] is not None:
                assert isinstance(job["payload"], dict)

    async def test_null_payload(self, client, jobs_pool_seeded):
        # The seed has one job with NULL payload — find it
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        any_null = any(j["payload"] is None for j in r.json()["jobs"])
        assert any_null

    async def test_oversized_payload_returns_null(self, client, jobs_pool_seeded):
        # The seed has one job with payload > 64 KiB; expect that row's payload to be null
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # All 500 jobs returned; oversized one is one of them
        # Verify no payload field exceeds the cap (a passed-through giant string would fail extra="forbid")
        for job in r.json()["jobs"]:
            if job["payload"] is not None:
                # parsed JSON dict: each should serialize back to a reasonable size
                assert len(json.dumps(job["payload"])) < 70000  # parsed dict is bounded by cap

    async def test_malformed_payload_returns_null(self, client, jobs_pool_seeded):
        # Seed inserts one job with payload "{not valid json"
        # The corresponding job should have payload=null in the response
        r = await client.get(
            "/api/v1/jobs?error=json corrupt&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # NOTE: this uses ?error= which is NOT in the allow-list, so this would 400.
        # Instead query without filter and find the malformed-payload row.
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # Identify the job that had the "json corrupt" error — its payload should be null
        corrupt_rows = [
            j for j in r.json()["jobs"]
            if j["error"] is not None and "json corrupt" in j["error"]
        ]
        assert len(corrupt_rows) == 1
        assert corrupt_rows[0]["payload"] is None

    async def test_non_dict_payload_returns_null(self, client, jobs_pool_seeded):
        # Seed has one job with payload `[1, 2, 3]` (array); response should be null
        # since spec only exposes dict-shaped JSON
        r = await client.get(
            "/api/v1/jobs?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # Heuristic: find a sweep+succeeded with timestamp 2026-04-04
        candidates = [
            j for j in r.json()["jobs"]
            if j["kind"] == "sweep" and j["state"] == "succeeded"
            and j["started_at"] == "2026-04-04T00:00:00Z"
        ]
        assert len(candidates) == 1
        assert candidates[0]["payload"] is None

    async def test_error_truncated_to_200(self, client, jobs_pool_seeded, populated_pool):
        # Set one job's error to a long string and re-query
        long_err = "x" * 5000
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE jobs SET error = ? WHERE state = 'failed' LIMIT 1",
                (long_err,),
            )
        r = await client.get(
            "/api/v1/jobs?state=failed&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        long_rows = [j for j in r.json()["jobs"] if j["error"] and len(j["error"]) >= 200]
        assert any(len(j["error"]) == 200 for j in long_rows)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestJobsErrorPaths:
    async def test_unknown_filter_field_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?password=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "unknown filter field" in r.json()["detail"]

    async def test_unknown_op_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?kind_gte=prefill",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_unknown_sort_field_400(self, client, jobs_pool_seeded):
        r = await client.get(
            "/api/v1/jobs?sort=password:desc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_unauth_401(self, client, jobs_pool_seeded):
        r = await client.get("/api/v1/jobs")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Pool failure (UAT-3+BL6 pattern: structured 503)
# ---------------------------------------------------------------------------


class TestJobsPoolFailure:
    async def test_pool_error_returns_503(self, unit_app, client):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _FakeBrokenPool:
            async def read_one(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")
            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()
        r = await client.get(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}
```

- [ ] **Step 2: Run tests — verify red phase**

Run:
```bash
source .venv/bin/activate && pytest tests/api/test_jobs_router.py -q --no-header 2>&1 | tail -10
```
Expected: most tests fail. The endpoint doesn't exist yet → 404, but the middleware returns 401 first because there's no bearer-exempt for /jobs. So many tests will fail on assertion checks (`200 != 401` etc.). A few tests like `test_unauth_401` may accidentally pass.

---

## Task 3: Implement `routers/jobs.py` + wire main.py (green phase)

**Files:**
- Create: `src/orchestrator/api/routers/jobs.py`
- Modify: `src/orchestrator/api/main.py` (+1 import, +1 `include_router`)

- [ ] **Step 1: Write the router module**

Create `src/orchestrator/api/routers/jobs.py`:

```python
"""GET /api/v1/jobs — paginated list of orchestrator jobs (BL8 / Feature 9)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api._query_helpers import (
    FilterAllowList,
    FilterFieldSpec,
    QueryParamError,
    SortAllowList,
)
from orchestrator.api._query_helpers import SortField as _SortField
from orchestrator.api._query_helpers import (
    build_order_by_clause,
    build_where_clause,
    parse_filters,
    parse_pagination,
    parse_sort,
)
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool


# Spec D1, D5 constants
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
ERROR_TRUNCATE = 200
PAYLOAD_MAX_BYTES = 65536  # 64 KiB (UAT-4 S3-e parity)

# Default sort per spec D1: id:desc. User explicit "id" in either direction
# deduplicates the tie-breaker append (UAT-4 S2-B behavior).
DEFAULT_SORT = (_SortField(field="id", direction="desc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

JOBS_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "kind": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "game_id": FilterFieldSpec(ops={"eq"}, value_type=int),
        "platform": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "state": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "progress": FilterFieldSpec(ops={"gte", "lte"}, value_type=float),
        "source": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "started_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
        "finished_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
    }
)

JOBS_SORT_ALLOW_LIST = SortAllowList(
    fields={"id", "kind", "state", "progress", "started_at", "finished_at"}
)

# All schema columns listed explicitly so the SELECT is stable across
# future migrations.
_JOBS_COLUMNS = (
    "id, kind, game_id, platform, state, progress, source, "
    "started_at, finished_at, error, payload"
)

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    kind: Literal["prefill", "validate", "library_sync", "auth_refresh", "sweep"]
    game_id: int | None
    platform: Literal["steam", "epic"] | None
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: float | None
    source: Literal["scheduler", "cli", "gameshelf", "api"]
    started_at: str | None
    finished_at: str | None
    error: str | None
    payload: dict[str, Any] | None


class SortFieldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"]


class JobsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, dict[str, Any]]  # plain dict per UAT-4 S2-A
    applied_sort: list[SortFieldResponse]


class JobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jobs: list[JobResponse]
    meta: JobsMeta


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/v1", tags=["jobs"])


@router.get(
    "/jobs",
    response_model=JobListResponse,
    responses={
        200: {"description": "Paginated list of jobs"},
        400: {"description": "Bad query parameters"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List jobs",
    description=(
        "Returns the orchestrator jobs feed with filter, sort, and pagination. "
        "Default sort is id:desc (most recently created). Active jobs surface "
        "via ?state_in=queued,running. See spec "
        "docs/superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md "
        "for the full per-field filter + sort allow-list."
    ),
)
async def list_jobs(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    try:
        pagination = parse_pagination(
            request.query_params,
            default_limit=DEFAULT_LIMIT,
            max_limit=MAX_LIMIT,
        )
        filters = parse_filters(request.query_params, allow_list=JOBS_FILTER_ALLOW_LIST)
        sort = parse_sort(
            request.query_params,
            allow_list=JOBS_SORT_ALLOW_LIST,
            default=list(DEFAULT_SORT),
            tie_breaker=TIE_BREAKER,
        )
    except QueryParamError as e:
        return JSONResponse(content={"detail": str(e)}, status_code=400)

    where_sql, where_params = build_where_clause(filters, allow_list=JOBS_FILTER_ALLOW_LIST)
    order_sql = build_order_by_clause(sort, allow_list=JOBS_SORT_ALLOW_LIST)

    # nosem: S608 — identifiers from allow-list-validated literals only;
    # values are parameterized via `?`. See _query_helpers security invariants.
    count_sql = f"SELECT COUNT(*) AS total FROM jobs {where_sql}".strip()  # noqa: S608
    rows_sql = (
        f"SELECT {_JOBS_COLUMNS} FROM jobs {where_sql} {order_sql} LIMIT ? OFFSET ?"  # noqa: S608
    ).strip()
    rows_params = [*where_params, pagination.limit, pagination.offset]

    try:
        count_row = await pool.read_one(count_sql, where_params)
        rows = await pool.read_all(rows_sql, rows_params)
    except PoolError as e:
        _log.error("api.jobs.read_failed", reason=str(e))
        return JSONResponse(content={"detail": "database unavailable"}, status_code=503)

    total = int(count_row["total"]) if count_row else 0

    jobs: list[JobResponse] = []
    for row in rows:
        raw_payload = row["payload"]
        payload: dict[str, Any] | None
        if raw_payload is None:
            payload = None
        elif len(raw_payload) > PAYLOAD_MAX_BYTES:
            _log.warning(
                "api.jobs.payload_oversized",
                job_id=row["id"],
                size_bytes=len(raw_payload),
                cap=PAYLOAD_MAX_BYTES,
            )
            payload = None
        else:
            try:
                parsed = json.loads(raw_payload)
                payload = parsed if isinstance(parsed, dict) else None
            except (json.JSONDecodeError, TypeError, RecursionError) as e:
                _log.warning(
                    "api.jobs.payload_parse_failed",
                    job_id=row["id"],
                    reason=type(e).__name__,
                )
                payload = None

        raw_err = row["error"]
        err = raw_err[:ERROR_TRUNCATE] if raw_err else None

        jobs.append(
            JobResponse(
                id=row["id"],
                kind=row["kind"],
                game_id=row["game_id"],
                platform=row["platform"],
                state=row["state"],
                progress=row["progress"],
                source=row["source"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                error=err,
                payload=payload,
            )
        )

    # UAT-4 S2-A: plain-dict applied_filters from parsed filters directly
    applied_filters: dict[str, dict[str, Any]] = {
        field_name: dict(ops) for field_name, ops in filters.items()
    }
    applied_sort = [SortFieldResponse(field=s.field, direction=s.direction) for s in sort]

    body = JobListResponse(
        jobs=jobs,
        meta=JobsMeta(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            has_more=(pagination.offset + len(jobs) < total),
            applied_filters=applied_filters,
            applied_sort=applied_sort,
        ),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))
```

- [ ] **Step 2: Wire main.py**

Open `src/orchestrator/api/main.py`. Find the existing import:
```python
from orchestrator.api.routers.games import router as games_router
```
Add below it (alphabetical preserved):
```python
from orchestrator.api.routers.jobs import router as jobs_router
```

Find the existing `include_router` lines:
```python
    app.include_router(games_router)
```
Add below:
```python
    app.include_router(jobs_router)
```

- [ ] **Step 3: Run jobs tests — verify green phase**

Run:
```bash
source .venv/bin/activate && pytest tests/api/test_jobs_router.py -q --no-header 2>&1 | tail -8
```
Expected: all jobs tests pass.

- [ ] **Step 4: Run full project test suite — confirm no regressions**

Run:
```bash
source .venv/bin/activate && pytest -q --no-header 2>&1 | tail -3
```
Expected: ~506 tests passing (481 prior + ~25 new). 0 failures.

---

## Task 4: Mark Build Loop checkpoints

- [ ] **Step 1: tests_written**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:tests_written
```
Expected: `[OK] Step 'tests_written' completed for build_loop (1/6)`.

- [ ] **Step 2: tests_verified_failing**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing
```
Expected: `[OK] Step 'tests_verified_failing' completed for build_loop (2/6)`.

- [ ] **Step 3: implemented**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:implemented
```
Expected: `[OK] Step 'implemented' completed for build_loop (3/6)`.

---

## Task 5: Security audit pass

**Files:**
- Create: `docs/security-audits/bl8-f9-jobs-readonly-security-audit.md`

- [ ] **Step 1: ruff check + format**

Run:
```bash
source .venv/bin/activate && ruff check src/orchestrator/api/routers/jobs.py src/orchestrator/api/main.py tests/api/test_jobs_router.py tests/api/conftest.py
ruff format --check src/orchestrator/api/routers/jobs.py src/orchestrator/api/main.py tests/api/test_jobs_router.py tests/api/conftest.py
```
Expected: `All checks passed!` for both. If format flags files, run `ruff format <files>` then re-run `--check`.

- [ ] **Step 2: mypy --strict**

Run:
```bash
source .venv/bin/activate && mypy --strict src/orchestrator/api/routers/jobs.py src/orchestrator/api/main.py
```
Expected: `Success: no issues found in 2 source files`.

- [ ] **Step 3: semgrep OWASP**

Run:
```bash
source .venv/bin/activate && semgrep --config p/owasp-top-ten --error src/orchestrator/api/routers/jobs.py
```
Expected: `0 findings`.

- [ ] **Step 4: gitleaks**

Run:
```bash
gitleaks detect --no-banner --redact --source .
```
Expected: `no leaks found`.

- [ ] **Step 5: Write the security audit doc**

Create `docs/security-audits/bl8-f9-jobs-readonly-security-audit.md`:

```markdown
# Security Audit — BL8 jobs read-only endpoint

**Feature:** BL8-F9-jobs-readonly (Build Loop 8, Milestone B)
**Module:** `src/orchestrator/api/routers/jobs.py` (~210 LoC) + 2-line wire in `src/orchestrator/api/main.py`
**Audit date:** 2026-05-20
**Auditor:** self-review (Senior Security Engineer persona) + automated SAST + gitleaks
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-05-20 -->

## Scope

Post-implementation security review of:
- `routers/jobs.py` — handler + Pydantic models + per-endpoint filter/sort allow-lists
- 2-line wire-up in `main.py`

The audit inherits the BL5+BL6+BL7+UAT-3+UAT-4 substrate. The shared `_query_helpers.py` module's security guarantees apply unchanged.

## Methodology

1. Automated SAST: `semgrep --config p/owasp-top-ten --error` — 0 findings
2. gitleaks: full repo scan — 0 findings
3. ruff check + ruff format: clean
4. mypy --strict: clean
5. UAT-4 property-based SQL-injection test in `test_query_helpers.py::TestSqlInjectionResistance` still passes — covers the shared `build_where_clause` that BL8 uses
6. Manual review against TM-005 (SQL injection), TM-012 (log redaction), spec §6 risk register

## Findings

**SEV-1: 0**
**SEV-2: 0**
**SEV-3: 0**
**SEV-4: 0**

No findings. BL8 is structurally identical to BL7 — same composition of UAT-4-hardened helpers, different per-endpoint allow-list.

## Decisions D1-D14 walk

- **D1 default sort id:desc**: verified via `TestJobsSort::test_default_id_desc` + `test_default_applied_sort_dedup_no_tie_breaker`
- **D2 payload included as JSON**: verified via `TestJobsPayloadAndError::test_well_formed_payload_parsed`; oversized + malformed + non-dict cases all return null
- **D3 `_is_null` deferred**: no operator in allow-list; documented
- **D4 no derived fields**: response has only schema columns; no `duration_sec`/`age_sec`
- **D5 error truncated to 200**: verified via `test_error_truncated_to_200`
- **D6-D14 inherited**: covered by UAT-4 regression suite + BL7 unit tests

## Threat-model walk

- **TM-005 SQL injection**: MITIGATED inherited from UAT-4. All values via `?` placeholders; identifiers only from allow-list-validated literals. The Hypothesis property test in `test_query_helpers.py` covers the shared builder.
- **TM-012 log redaction**: MITIGATED. Endpoint emits `api.jobs.read_failed` (with PoolError str message — no raw rows), `api.jobs.payload_oversized` (job_id + size only), `api.jobs.payload_parse_failed` (job_id + error type only). No payload content reaches a log call.
- **TM-013 fingerprinting**: MITIGATED. Same 200/400/503 surface as BL7.

## Non-findings (cleared)

- No SQL injection vector.
- No timing oracle on auth (middleware-gated).
- No log-volume amplification on the hot path.
- No DoS via response size (limit ≤ 500 enforced; payload bounded at 64 KiB per row; error truncated to 200 chars).
- payload schema-comment promise ("NEVER contains credentials") is the upstream contract; UAT-4 size+parse defenses cap the blast radius if violated.

## Test coverage

~25 tests in `tests/api/test_jobs_router.py` across 9 classes. Branch coverage on `routers/jobs.py` ≥95%.

## Verification artifacts

- `pytest -q`: ~506 tests passing project-wide (was 481; +25)
- `ruff check` + `ruff format --check`: clean
- `mypy --strict`: clean
- `semgrep --config p/owasp-top-ten --error`: 0 findings
- `gitleaks detect`: no leaks

## Conclusion

**APPROVED for merge.** Zero findings. BL8 is the cheap-propagation proof point: composing UAT-4-hardened helpers + a per-endpoint allow-list produces a secure-by-default new endpoint with no shared-module changes.
```

- [ ] **Step 6: Mark security_audit**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:security_audit
```
Expected: `[OK] Step 'security_audit' completed for build_loop (4/6)`.

---

## Task 6: Update CHANGELOG + FEATURES

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `FEATURES.md`

- [ ] **Step 1: CHANGELOG entry**

Open `CHANGELOG.md`. Find `## [Unreleased]` → `### Added`. Add as FIRST item under `### Added`:

```markdown
- **`GET /api/v1/jobs`** (BL8 / Feature 9 partial) — second paginated F9
  read endpoint. Returns the orchestrator jobs feed with filter, sort,
  and pagination. Default sort `id:desc` (most-recently-created first);
  active jobs surface via `?state_in=queued,running`. Per-endpoint
  filterable: `kind`, `game_id`, `platform`, `state`, `progress` (range),
  `source`, `started_at`/`finished_at` (range). Sortable: `id`, `kind`,
  `state`, `progress`, `started_at`, `finished_at`. `payload` JSON column
  included as parsed dict (UAT-4 hardening: 64 KiB cap + RecursionError
  catch + null on parse failure); `error` truncated to 200 chars.
  Validates the proposition that BL7+UAT-4-hardened `_query_helpers.py`
  conventions propagate cheaply — zero changes to the shared module.
  See [spec](docs/superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md)
  and [audit](docs/security-audits/bl8-f9-jobs-readonly-security-audit.md).
```

- [ ] **Step 2: FEATURES Feature 8 entry**

Open `FEATURES.md`. After the existing Feature 7 (BL7 games) section, add:

```markdown
## Feature 8: BL8 — `GET /api/v1/jobs` (read-only, paginated)

**Phase Built:** 2 (Milestone B, Build Loop 8)
**Status:** Complete (2026-05-20)
**Summary:** Second paginated F9 read endpoint. Returns the orchestrator
jobs feed with filter, sort, and offset-based pagination. Inherits BL7's
wrapped envelope `{"jobs": [...], "meta": {...}}` with all UAT-4
hardening (compact applied_filters echo, INT64 range checks, _in
cardinality cap, identifier validation, etc.). Default sort `id:desc`
keeps active jobs surfaced via explicit `?state_in=queued,running`
filter (the canonical Game_shelf active-jobs query).
**Key Interfaces:**
  - `src/orchestrator/api/routers/jobs.py` — `JobResponse`,
    `JobListResponse`, `JobsMeta`, `SortFieldResponse` Pydantic models;
    `JOBS_FILTER_ALLOW_LIST`, `JOBS_SORT_ALLOW_LIST`; `list_jobs` handler
  - Wired in `src/orchestrator/api/main.py` via
    `app.include_router(jobs_router)`
**Locked decisions (D1-D14):** id:desc default sort · payload as parsed
JSON · _is_null deferred · no derived fields · error 200-char truncation
· remaining D6-D14 inherited from BL7+UAT-4. See
[spec](superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md).
**Test Coverage:** ~25 tests in `tests/api/test_jobs_router.py` (~480 LoC)
across 9 classes: empty DB, happy path, pagination, enum filters, scalar
filters, timestamp filters, sort (incl. tie-breaker dedup), applied echo,
payload+error handling, error paths, pool failure. Plus `jobs_pool_seeded`
fixture in `conftest.py` (~50 jobs across all enum combinations).
**Related Audit:** [`bl8-f9-jobs-readonly-security-audit.md`](security-audits/bl8-f9-jobs-readonly-security-audit.md) — 0 findings.
**Known Limitations:**
  - No `_is_null` operator — orphan-job queries (`?game_id_is_null=true`)
    require direct DB access until a real Game_shelf need surfaces.
  - No derived fields — `duration_sec` is client-derivable from
    `started_at` + `finished_at`; `age_sec` would break response
    determinism.
  - `?source=...` queries are full-table-scan (no index). At expected
    scale (thousands of rows) this is acceptable; `idx_jobs_source` is a
    future migration if it becomes a hot path.

---
```

- [ ] **Step 3: Mark documentation_updated**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
```
Expected: `[OK] Step 'documentation_updated' completed for build_loop (5/6)`.

---

## Task 7: Combined feat + docs commit

**Files staged:** all source + tests + CHANGELOG + FEATURES + audit doc + `.claude/process-state.json` (auto-bumped by checklist marks).

- [ ] **Step 1: Survey state**

Run:
```bash
git status --short
git diff --stat
```

- [ ] **Step 2: Stage all files**

Run:
```bash
git add \
  src/orchestrator/api/routers/jobs.py \
  src/orchestrator/api/main.py \
  tests/api/test_jobs_router.py \
  tests/api/conftest.py \
  docs/security-audits/bl8-f9-jobs-readonly-security-audit.md \
  CHANGELOG.md \
  FEATURES.md \
  .claude/process-state.json
```

- [ ] **Step 3: Write commit message to tmp**

Write `/tmp/bl8-feat-commit.txt`:
```
feat(api): GET /api/v1/jobs — second paginated F9 read endpoint

Composes the BL7+UAT-4-hardened _query_helpers.py with a
per-endpoint allow-list. Zero changes to the shared module —
validates that future paginated F9 endpoints can be added cheaply.

Behavior (14 locked decisions D1-D14):
- D1 Default sort id:desc (active jobs surface via
  ?state_in=queued,running filter)
- D2 payload column included as parsed JSON (UAT-4 64 KiB cap +
  RecursionError catch + null on parse failure)
- D3 _is_null operator deferred (Game_shelf doesn't need it yet)
- D4 No derived fields (duration_sec client-derivable;
  age_sec would break response determinism)
- D5 error truncated to 200 chars (BL6/BL7 pattern)
- D6-D14 INHERITED from BL7+UAT-4 (envelope, pagination, filter
  syntax, sort syntax, applied_* echo, auth, pool error, Pydantic
  strictness, etc.)

Per-endpoint allow-list:
- Filterable: kind (eq,_in), game_id (eq), platform (eq,_in),
  state (eq,_in), progress (gte,lte), source (eq,_in),
  started_at + finished_at (gte,lte; ISO 8601 validator)
- Sortable: id, kind, state, progress, started_at, finished_at

Implementation:
- src/orchestrator/api/routers/jobs.py (~210 LoC) — Pydantic models
  + handler composing the shared helpers
- 2-line wire-up in main.py
- jobs_pool_seeded fixture in tests/api/conftest.py (~50 jobs across
  all enum combinations, including 1 oversized + 1 malformed +
  1 non-dict payload for parse-path tests)

Tests:
- tests/api/test_jobs_router.py — ~25 tests across 9 classes
  (empty DB, happy path, pagination, enum filters, scalar filters,
  timestamp filters, sort + tie-breaker dedup, applied echo,
  payload+error handling, error paths, pool failure)

Docs:
- CHANGELOG entry under [Unreleased] -> Added
- FEATURES Feature 8 entry
- Security audit at
  docs/security-audits/bl8-f9-jobs-readonly-security-audit.md
  (0 findings)
- Spec at
  docs/superpowers/specs/2026-05-20-bl8-jobs-readonly-design.md
  (committed 2db87b0)
- Plan at
  docs/superpowers/plans/2026-05-20-bl8-jobs-readonly.md
  (separate docs(plan) commit)

Verification: full project suite green (~506 tests; +25 new);
ruff / ruff format / mypy --strict / semgrep p/owasp-top-ten /
gitleaks all clean.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

- [ ] **Step 4: Mark evaluated + commit**

Run:
```bash
bash .claude/framework/hooks/mark-evaluated.sh "BL8 feat+docs commit — single bundled per BL6/BL7 pattern; user pre-approved end-to-end via autonomy grant"
git commit -F /tmp/bl8-feat-commit.txt
```

- [ ] **Step 5: Verify**

Run:
```bash
git log --oneline -3
git status --short
```
Expected: top commit subject `feat(api): GET /api/v1/jobs — second paginated F9 read endpoint`. Working tree clean.

---

## Task 8: Mark feature_recorded + record in test-gate counter

- [ ] **Step 1: Mark feature_recorded**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:feature_recorded
```
Expected: `[OK] Step 'feature_recorded' completed for build_loop (6/6)` + `[OK] All steps complete for build_loop!`.

- [ ] **Step 2: Record feature in test-gate counter**

Run:
```bash
scripts/test-gate.sh --record-feature "BL8-F9-jobs-readonly"
```
Expected: counter increments to 1/2 toward UAT-5.

- [ ] **Step 3: Verify state**

Run:
```bash
scripts/test-gate.sh --check-batch
```
Expected: `[OK] Clear to continue (1 features until next testing session)`.

---

## Task 9: Push + open PR

- [ ] **Step 1: Push branch**

Run:
```bash
git push -u origin feat/bl8-jobs-readonly
```

- [ ] **Step 2: Write PR body**

Write `/tmp/bl8-pr-body.txt`:
```markdown
## Summary

BL8 — `GET /api/v1/jobs` — second paginated F9 read endpoint. **Zero changes to the shared `_query_helpers.py`** — composes BL7+UAT-4-hardened primitives with a per-endpoint allow-list. Validates that future paginated F9 endpoints can be added cheaply.

## What's in this PR

| Commit | Purpose |
|---|---|
| `docs(spec)` | Design with 14 locked decisions (10 inherited from BL7+UAT-4, 4 jobs-specific) |
| `docs(plan)` | 9-task implementation plan |
| `feat(api)` | Router + tests + fixture + CHANGELOG/FEATURES/audit |

## Locked decisions (jobs-specific)

| ID | Decision |
|---|---|
| D1 | Default sort `id:desc` (active jobs surface via `?state_in=queued,running` filter) |
| D2 | `payload` column included as parsed JSON (UAT-4 hardening applies) |
| D3 | `_is_null` operator deferred |
| D4 | No derived fields (duration_sec / age_sec) |
| D5 | `error` truncated to 200 chars at API layer |

(D6-D14 inherited from BL7+UAT-4.)

## Per-endpoint allow-list

**Filterable** (per operator-suffix syntax):
- `kind`, `state`, `platform`, `source` — eq + `_in`
- `game_id` — eq
- `progress` — `_gte`, `_lte`
- `started_at`, `finished_at` — `_gte`, `_lte` (ISO 8601 validator)

**Sortable:** `id`, `kind`, `state`, `progress`, `started_at`, `finished_at`. Default `id:desc`.

## Verification

- ~506 project tests passing (+25 new in `test_jobs_router.py`)
- ruff / ruff format / mypy --strict / semgrep p/owasp-top-ten / gitleaks all clean
- 6/6 Build Loop checklist; feature recorded; counter at 1/2 toward UAT-5
- Security audit: 0 findings

## Test plan

- [ ] CI status checks pass (8 required)
- [ ] Manual smoke (bearer as `$T`):
  - `curl ... '/api/v1/jobs?limit=5'` returns wrapped envelope; default sort `id:desc`
  - `curl ... '/api/v1/jobs?state_in=queued,running'` returns active jobs only
  - `curl ... '/api/v1/jobs?kind=prefill&progress_gte=0.9'` returns almost-done prefill jobs
  - `curl ... '/api/v1/jobs?password=foo'` returns 400
  - `curl ... '/api/v1/jobs?started_at_gte=<script>'` returns 400 (timestamp validator regression)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 3: Open PR**

Run:
```bash
gh pr create \
  --title "feat(api): GET /api/v1/jobs — second paginated F9 read endpoint" \
  --body-file /tmp/bl8-pr-body.txt \
  --base main \
  --head feat/bl8-jobs-readonly
```

- [ ] **Step 4: Report PR URL; do NOT merge**

Per `feedback_pr_merge_ownership.md`: user merges PRs themselves. Stop after PR is opened.

---

## Self-Review

**Spec coverage check:**

| Spec section | Plan task |
|---|---|
| §2 D1 default sort id:desc | Task 2 `TestJobsSort::test_default_id_desc`; Task 3 `DEFAULT_SORT` constant |
| §2 D2 payload as parsed JSON | Task 2 `TestJobsPayloadAndError`; Task 3 payload parse block |
| §2 D3 `_is_null` deferred | No allow-list op; documented in Task 6 FEATURES known-limitations |
| §2 D4 no derived fields | `JobResponse` schema in Task 3 has no `duration_sec`/`age_sec` |
| §2 D5 error truncation | Task 2 `test_error_truncated_to_200`; Task 3 `[:ERROR_TRUNCATE]` |
| §2 D6-D14 (inherited) | All covered by composition of `_query_helpers.py` primitives |
| §3.1 per-field allow-list | Task 3 `JOBS_FILTER_ALLOW_LIST` constant |
| §3.2 response shape | Task 3 Pydantic models match wire format |
| §3.3 error responses | Task 3 try/except returns 400/503 |
| §4 architecture | Task 3 file paths + structure |
| §4.3 index utilization | (informational; no plan task needed) |
| §5 test plan | Task 2 — all 9 test classes |
| §6 risk register | Tests cover injection (via UAT-4 property test), payload parse (D2 case), `extra="forbid"` migration drift |
| §7 documentation deltas | Task 5 (audit) + Task 6 (CHANGELOG/FEATURES) |

**Placeholder scan:** No TBD/TODO. All file paths, code blocks, commands concrete.

**Type consistency:** `_SortField` (helpers, dataclass) vs `SortFieldResponse` (Pydantic model in router) intentionally separate; conversion in handler — same pattern as BL7. `JOBS_FILTER_ALLOW_LIST` referenced consistently across Task 2 (tests) and Task 3 (impl). `ERROR_TRUNCATE`, `PAYLOAD_MAX_BYTES`, `DEFAULT_LIMIT`, `MAX_LIMIT`, `DEFAULT_SORT`, `TIE_BREAKER` all defined in Task 3 and referenced consistently downstream.
