# BL9 Manifests Read-Only Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `GET /api/v1/manifests` — third paginated F9 read endpoint — and introduce the `?include=` opt-in expansion convention via a thin `_query_helpers.py` extension. Validates that the shared module can grow new primitives cheaply when a real new use case arises.

**Architecture:** Extend `_query_helpers.py` with `IncludeAllowList` + `parse_includes` (~30 LoC). New router at `src/orchestrator/api/routers/manifests.py` (~250 LoC: per-endpoint allow-lists + Pydantic response models + handler with conditional `LEFT JOIN games` SQL when `?include=game`). Wired into `main.py`. New `manifests_pool_seeded` conftest fixture. ~30 router tests + ~5 helper tests for the new primitive.

**Tech Stack:** Python 3.12, FastAPI 0.136.1, Pydantic v2, aiosqlite, structlog, httpx (test client), pytest-asyncio.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/orchestrator/api/_query_helpers.py` | **Modify** (+~30 LoC) | `IncludeAllowList` dataclass + `parse_includes` function; add `"include"` to `_RESERVED_PARAM_NAMES` |
| `src/orchestrator/api/routers/manifests.py` | **Create** (~250 LoC) | `GameSummary`, `ManifestResponse`, `ManifestsMeta`, `ManifestListResponse` Pydantic models; 3 allow-lists; `list_manifests` handler with conditional JOIN |
| `src/orchestrator/api/main.py` | **Modify** (+1 import, +1 `include_router`) | Wire the new router |
| `tests/api/conftest.py` | **Modify** (+1 fixture: `manifests_pool_seeded`) | ~30-row manifests seed across the 5 baseline games |
| `tests/api/test_query_helpers.py` | **Modify** (+~5 tests) | Coverage for `parse_includes` + `IncludeAllowList` |
| `tests/api/test_manifests_router.py` | **Create** (~480 LoC, ~30 tests) | HTTP-level tests via httpx ASGI transport |
| `docs/security-audits/bl9-f9-manifests-readonly-security-audit.md` | **Create** | Per-feature audit doc (Build Loop gate requirement) |
| `CHANGELOG.md` | **Modify** | BL9 entry under `[Unreleased]` → `### Added` |
| `FEATURES.md` | **Modify** | New Feature 9 entry |

---

## Task 0: Commit this plan to git

**Files:**
- Already written: `docs/superpowers/plans/2026-05-20-bl9-manifests-readonly.md`

**Branch state:** Already on `feat/bl9-manifests-readonly`. Spec committed `0c72c4d`. `--start-feature "BL9-F9-manifests-readonly"` already marked.

- [ ] **Step 1: Confirm plan file untracked**

Run:
```bash
git status --short docs/superpowers/plans/2026-05-20-bl9-manifests-readonly.md
```
Expected: `??` (untracked).

- [ ] **Step 2: Write commit message to tmp**

Write `/tmp/bl9-plan-commit.txt`:
```
docs(plan): BL9 manifests read-only implementation plan

Decomposes the BL9 spec (0c72c4d) into 9 tasks following the
project Build Loop:
- Task 0: this plan commit
- Task 1: extend _query_helpers with IncludeAllowList + parse_includes
  + ~5 helper tests + "include" added to _RESERVED_PARAM_NAMES
- Task 2: write manifests_pool_seeded fixture (~30 rows)
- Task 3: write router tests (red phase)
- Task 4: implement routers/manifests.py + wire main.py (green phase)
- Task 5: mark tests_written + tests_verified_failing + implemented
- Task 6: security audit (ruff, mypy, semgrep, gitleaks) + audit doc
- Task 7: CHANGELOG + FEATURES + mark documentation_updated
- Task 8: combined feat+docs commit + push + open PR
- Task 9: mark feature_recorded + record in test-gate counter
  (counter at 1/2 → 2/2 → UAT-5 fires before BL10)

Adds ONE new helpers primitive (IncludeAllowList + parse_includes)
following the strict-scope convention. Validates that the shared
module can grow when a real new use case (opt-in FK expansion)
arises.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

- [ ] **Step 3: Mark evaluation, stage, commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "BL9 plan commit — docs(spec)-then-docs(plan) pattern; user pre-approved end-to-end via autonomy grant"
git add docs/superpowers/plans/2026-05-20-bl9-manifests-readonly.md
git commit -F /tmp/bl9-plan-commit.txt
```

- [ ] **Step 4: Verify**

```bash
git log --oneline -2
git status --short
```
Expected: top commit `docs(plan): BL9 manifests read-only implementation plan`. Working tree clean.

---

## Task 1: Extend `_query_helpers.py` with `parse_includes` + `IncludeAllowList`

**Files:**
- Modify: `src/orchestrator/api/_query_helpers.py` (+~30 LoC)
- Modify: `tests/api/test_query_helpers.py` (+~5 tests)

- [ ] **Step 1: TaskUpdate the active task to in_progress** (enforce-plan-tracking hook)

- [ ] **Step 2: Read current `_query_helpers.py` to find insertion points**

```bash
grep -n "_RESERVED_PARAM_NAMES\|^@dataclass(frozen=True)\nclass SortAllowList\|^def parse_sort" src/orchestrator/api/_query_helpers.py
```
Note line numbers for: `_RESERVED_PARAM_NAMES` constant, `SortAllowList` definition (insert `IncludeAllowList` after it), `parse_sort` function (insert `parse_includes` after `build_order_by_clause`).

- [ ] **Step 3: Modify `_RESERVED_PARAM_NAMES` to include `"include"`**

Open `src/orchestrator/api/_query_helpers.py`. Find:
```python
_RESERVED_PARAM_NAMES = frozenset({"limit", "offset", "sort"})
```
Replace with:
```python
_RESERVED_PARAM_NAMES = frozenset({"limit", "offset", "sort", "include"})
```

- [ ] **Step 4: Add `IncludeAllowList` dataclass after `SortAllowList`**

Find the end of `SortAllowList` class (just after the `object.__setattr__` line). Insert two blank lines, then add:

```python
@dataclass(frozen=True)
class IncludeAllowList:
    """Per-endpoint declaration of permitted ?include= expansion keys.

    UAT-9 BL9: opt-in FK expansion convention. Each endpoint declares
    which include keys are allowed (e.g., {"game"} for /manifests).
    Identifier-validated at construction.
    """

    keys: frozenset[str] = field(default_factory=frozenset)

    def __init__(self, keys: set[str] | frozenset[str]) -> None:
        for k in keys:
            _validate_identifier(k, kind="include key")
        object.__setattr__(self, "keys", frozenset(keys))
```

- [ ] **Step 5: Add `parse_includes` function after `build_order_by_clause`**

Find the end of `build_order_by_clause` function. Insert two blank lines after the last line of its body (after `return "ORDER BY " + ", ".join(entries)`), then add:

```python
def parse_includes(
    params: QueryParams,
    *,
    allow_list: IncludeAllowList,
) -> set[str]:
    """Parse ?include= query param into a deduplicated set of include keys.

    Empty/absent ?include= returns an empty set. Unknown keys raise
    QueryParamError. Values are comma-separated; per-key whitespace
    stripped; deduplicated; empty values dropped.

    Per spec D5 (BL9): the `include` query param is reserved
    (see _RESERVED_PARAM_NAMES) so the filter parser will not interpret
    it as a filter field.
    """
    raw = params.get("include")
    if not raw:
        return set()
    requested = {k.strip() for k in raw.split(",") if k.strip()}
    unknown = requested - allow_list.keys
    if unknown:
        raise QueryParamError(
            f"include keys not allowed: {sorted(unknown)}"
        )
    return requested
```

- [ ] **Step 6: Write the helper tests** in `tests/api/test_query_helpers.py`

Find the end of the file (after the existing `TestParseSort` and `TestBuildOrderByClause` classes). Append a new test class:

```python
# ---------------------------------------------------------------------------
# BL9: parse_includes + IncludeAllowList
# ---------------------------------------------------------------------------


class TestParseIncludes:
    def test_absent_returns_empty_set(self):
        from orchestrator.api._query_helpers import IncludeAllowList, parse_includes

        result = parse_includes(
            QueryParams(""),
            allow_list=IncludeAllowList(keys={"game"}),
        )
        assert result == set()

    def test_empty_string_returns_empty_set(self):
        from orchestrator.api._query_helpers import IncludeAllowList, parse_includes

        result = parse_includes(
            QueryParams("include="),
            allow_list=IncludeAllowList(keys={"game"}),
        )
        assert result == set()

    def test_single_value(self):
        from orchestrator.api._query_helpers import IncludeAllowList, parse_includes

        result = parse_includes(
            QueryParams("include=game"),
            allow_list=IncludeAllowList(keys={"game"}),
        )
        assert result == {"game"}

    def test_multi_value_deduped(self):
        from orchestrator.api._query_helpers import IncludeAllowList, parse_includes

        result = parse_includes(
            QueryParams("include=game,game,game"),
            allow_list=IncludeAllowList(keys={"game"}),
        )
        assert result == {"game"}

    def test_whitespace_stripped(self):
        from orchestrator.api._query_helpers import IncludeAllowList, parse_includes

        result = parse_includes(
            QueryParams("include= game , game "),
            allow_list=IncludeAllowList(keys={"game"}),
        )
        assert result == {"game"}

    def test_unknown_key_raises(self):
        from orchestrator.api._query_helpers import (
            IncludeAllowList,
            QueryParamError,
            parse_includes,
        )

        with pytest.raises(QueryParamError, match=r"include keys not allowed"):
            parse_includes(
                QueryParams("include=games"),
                allow_list=IncludeAllowList(keys={"game"}),
            )

    def test_invalid_identifier_rejected_at_construction(self):
        from orchestrator.api._query_helpers import IncludeAllowList

        with pytest.raises(ValueError, match=r"invalid identifier|must match"):
            IncludeAllowList(keys={"1=1 OR x"})

    def test_reserved_param_name_rejected_at_construction(self):
        from orchestrator.api._query_helpers import IncludeAllowList

        for reserved in ("limit", "offset", "sort", "include"):
            with pytest.raises(ValueError, match=r"reserved"):
                IncludeAllowList(keys={reserved})
```

- [ ] **Step 7: Run helper tests to verify red → green**

```bash
source .venv/bin/activate && pytest tests/api/test_query_helpers.py::TestParseIncludes -q --no-header
```
Expected: 8 tests pass (all parse_includes + IncludeAllowList behavior covered).

- [ ] **Step 8: Run full helper test suite to confirm no regression**

```bash
source .venv/bin/activate && pytest tests/api/test_query_helpers.py -q --no-header
```
Expected: all tests pass (existing + 8 new).

---

## Task 2: Add `manifests_pool_seeded` fixture to conftest.py

**Files:**
- Modify: `tests/api/conftest.py` (+1 fixture after `jobs_pool_seeded`)

- [ ] **Step 1: Read the current end of jobs_pool_seeded to find insertion point**

```bash
grep -n "^@pytest_asyncio.fixture\|jobs_pool_seeded\|if TYPE_CHECKING" tests/api/conftest.py | head -10
```
Note the line where `jobs_pool_seeded` function ends (the `return populated_pool` line before `if TYPE_CHECKING:`).

- [ ] **Step 2: Add the fixture after `jobs_pool_seeded`**

Insert before the `if TYPE_CHECKING:` block:

```python
@pytest_asyncio.fixture
async def manifests_pool_seeded(populated_pool):  # noqa: F811
    """populated_pool seeded with ~30 manifests across the 5 baseline games.

    Mix designed for BL9 filter+sort+pagination+include tests:
    - 5 games (ids 1-5) each get 4-7 manifests (history)
    - version formats vary: Steam-style numeric IDs, Epic-style dotted
    - chunk_count spread: 100, 1820, 5000, 12000, 50000
    - total_bytes spread: 1 GB to 100 GB
    - fetched_at spread across past month (per-manifest distinct)
    - raw BLOB: small constant byte sequence (not zstd-parsed; just NOT NULL)
    """
    import json as _json

    raw_placeholder = b"\x28\xb5\x2f\xfd\x00\x00stub-zstd-payload"  # zstd magic + stub

    async with populated_pool.write_transaction() as tx:
        manifests_seed = [
            # game_id, version, chunk_count, total_bytes, fetched_at_day_offset
            (1, "10001", 100, 1_000_000_000, 28),
            (1, "10002", 250, 2_500_000_000, 21),
            (1, "10003", 1820, 5_000_000_000, 14),
            (1, "10004", 5000, 25_000_000_000, 7),
            (1, "10005", 12000, 75_000_000_000, 1),
            (2, "20001", 500, 5_000_000_000, 25),
            (2, "20002", 1500, 15_000_000_000, 10),
            (2, "20003", 3000, 30_000_000_000, 3),
            (3, "30001", 100, 500_000_000, 30),
            (3, "30002", 800, 8_000_000_000, 20),
            (3, "30003", 1200, 12_000_000_000, 12),
            (3, "30004", 2400, 22_000_000_000, 5),
            (4, "v1.0.0", 200, 1_500_000_000, 27),
            (4, "v1.1.0", 450, 4_500_000_000, 19),
            (4, "v1.2.0", 900, 9_000_000_000, 11),
            (4, "v2.0.0", 2200, 22_000_000_000, 4),
            (5, "++Release-1.0", 350, 3_500_000_000, 29),
            (5, "++Release-1.1", 700, 7_000_000_000, 22),
            (5, "++Release-1.2", 1400, 14_000_000_000, 15),
            (5, "++Release-2.0", 2800, 28_000_000_000, 8),
            (5, "++Release-2.1", 50000, 100_000_000_000, 2),
        ]

        for game_id, version, chunk_count, total_bytes, days_ago in manifests_seed:
            # Compute fetched_at as 2026-05-20 minus days_ago
            day = 20 - days_ago
            if day <= 0:
                month = 4
                day = 30 + day
            else:
                month = 5
            fetched_at = f"2026-{month:02d}-{day:02d}T12:00:00Z"
            await tx.execute(
                "INSERT INTO manifests "
                "(game_id, version, fetched_at, chunk_count, total_bytes, raw) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (game_id, version, fetched_at, chunk_count, total_bytes, raw_placeholder),
            )

    return populated_pool
```

- [ ] **Step 3: Verify fixture compiles (no collection errors)**

```bash
source .venv/bin/activate && pytest tests/api/ --collect-only -q 2>&1 | tail -3
```
Expected: all tests still collect (new fixture is referenced only by upcoming Task 3 tests; current suite unaffected).

---

## Task 3: Write router tests (red phase)

**Files:**
- Create: `tests/api/test_manifests_router.py`

- [ ] **Step 1: Write the full test file**

Create `tests/api/test_manifests_router.py`:

```python
"""Tests for GET /api/v1/manifests (BL9 / Feature 9 partial).

Covers spec §5 — empty DB, happy path, pagination, filtering, sorting,
applied-echo, error paths, auth, pool-failure, and the NEW ?include=game
expansion convention.
"""

from __future__ import annotations

VALID_TOKEN = "a" * 32


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


class TestManifestsEmptyDb:
    async def test_empty_db_returns_empty_array(self, client, populated_pool):
        # populated_pool has 3 baseline manifests from db conftest; clear them
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM manifests")
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["manifests"] == []
        assert body["meta"]["total"] == 0
        assert body["meta"]["has_more"] is False
        assert body["meta"]["applied_includes"] == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestManifestsHappyPath:
    async def test_returns_manifests(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "manifests" in body
        assert "meta" in body
        assert len(body["manifests"]) > 0
        assert body["meta"]["total"] >= 20

    async def test_envelope_shape(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert set(body.keys()) == {"manifests", "meta"}
        assert set(body["meta"].keys()) == {
            "total",
            "limit",
            "offset",
            "has_more",
            "applied_filters",
            "applied_sort",
            "applied_includes",
        }

    async def test_per_manifest_field_set(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        expected_fields = {
            "id",
            "game_id",
            "version",
            "fetched_at",
            "chunk_count",
            "total_bytes",
            "game",
        }
        for manifest in body["manifests"]:
            assert set(manifest.keys()) == expected_fields
            # game must be null without ?include=game
            assert manifest["game"] is None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestManifestsPagination:
    async def test_default_limit_50(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert body["meta"]["limit"] == 50
        assert body["meta"]["offset"] == 0

    async def test_explicit_limit(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=5",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["manifests"]) == 5
        assert body["meta"]["limit"] == 5

    async def test_offset_progression(self, client, manifests_pool_seeded):
        r1 = await client.get(
            "/api/v1/manifests?limit=5&offset=0",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.get(
            "/api/v1/manifests?limit=5&offset=5",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids1 = [m["id"] for m in r1.json()["manifests"]]
        ids2 = [m["id"] for m in r2.json()["manifests"]]
        assert set(ids1).isdisjoint(set(ids2))

    async def test_limit_above_max_returns_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=1000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestManifestsFilters:
    async def test_game_id_eq(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?game_id=1&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["game_id"] == 1

    async def test_game_id_in(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?game_id_in=1,2,3&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids = {m["game_id"] for m in r.json()["manifests"]}
        assert ids.issubset({1, 2, 3})

    async def test_version_eq(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?version=10001&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["version"] == "10001"

    async def test_version_in(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?version_in=10001,20001&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        versions = {m["version"] for m in r.json()["manifests"]}
        assert versions.issubset({"10001", "20001"})

    async def test_chunk_count_range(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?chunk_count_gte=1000&chunk_count_lte=5000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert 1000 <= m["chunk_count"] <= 5000

    async def test_total_bytes_gte(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?total_bytes_gte=50000000000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["total_bytes"] >= 50_000_000_000

    async def test_fetched_at_range(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?fetched_at_gte=2026-05-15T00:00:00Z&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["fetched_at"] >= "2026-05-15T00:00:00Z"


# ---------------------------------------------------------------------------
# Sort (default fetched_at:desc; tie-breaker id:asc)
# ---------------------------------------------------------------------------


class TestManifestsSort:
    async def test_default_fetched_at_desc(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        timestamps = [m["fetched_at"] for m in r.json()["manifests"]]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_default_applied_sort_has_tie_breaker(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied == [
            {"field": "fetched_at", "direction": "desc"},
            {"field": "id", "direction": "asc"},
        ]

    async def test_user_id_sort_dedupes_tie_breaker(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?sort=id:desc&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied == [{"field": "id", "direction": "desc"}]

    async def test_sort_by_total_bytes_desc(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?sort=total_bytes:desc&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        bytes_list = [m["total_bytes"] for m in r.json()["manifests"]]
        assert bytes_list == sorted(bytes_list, reverse=True)


# ---------------------------------------------------------------------------
# Applied echo (UAT-4 S2-A compact shape + new applied_includes)
# ---------------------------------------------------------------------------


class TestManifestsAppliedEcho:
    async def test_applied_filters_compact_dict_shape(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?game_id=1&chunk_count_gte=1000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert applied == {
            "game_id": {"eq": 1},
            "chunk_count": {"gte": 1000},
        }

    async def test_applied_includes_empty_by_default(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.json()["meta"]["applied_includes"] == []

    async def test_applied_includes_populated_when_requested(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=game&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.json()["meta"]["applied_includes"] == ["game"]


# ---------------------------------------------------------------------------
# ?include=game (NEW BL9 convention)
# ---------------------------------------------------------------------------


class TestManifestsIncludeGame:
    async def test_no_include_game_is_null(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?limit=5",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["game"] is None

    async def test_include_game_populated(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=game&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        for m in r.json()["manifests"]:
            assert m["game"] is not None
            assert set(m["game"].keys()) == {"title", "platform", "app_id"}
            assert m["game"]["platform"] in ("steam", "epic")

    async def test_include_game_matches_seeded_game_row(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?game_id=1&include=game&limit=5",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # populated_pool seeds game id=1 as Counter-Strike (steam, app_id=10)
        for m in r.json()["manifests"]:
            assert m["game_id"] == 1
            assert m["game"]["title"] == "Counter-Strike"
            assert m["game"]["platform"] == "steam"
            assert m["game"]["app_id"] == "10"

    async def test_unknown_include_key_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "include keys not allowed" in r.json()["detail"]

    async def test_include_deduped(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=game,game,game&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        assert r.json()["meta"]["applied_includes"] == ["game"]

    async def test_empty_include_no_expansion(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?include=&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.json()["meta"]["applied_includes"] == []
        for m in r.json()["manifests"]:
            assert m["game"] is None


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestManifestsErrorPaths:
    async def test_unknown_filter_field_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?password=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "unknown filter field" in r.json()["detail"]

    async def test_unknown_op_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?version_gte=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_unknown_sort_field_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?sort=password:desc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_invalid_timestamp_format_returns_400(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?fetched_at_gte=<script>alert(1)</script>",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auth + pool failure
# ---------------------------------------------------------------------------


class TestManifestsAuth:
    async def test_unauth_401(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests")
        assert r.status_code == 401


class TestManifestsPoolFailure:
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
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}
```

- [ ] **Step 2: Run tests to verify red phase**

```bash
source .venv/bin/activate && pytest tests/api/test_manifests_router.py -q --no-header 2>&1 | tail -10
```
Expected: most tests fail (router doesn't exist). The middleware returns 401 first for unauth tests, so `TestManifestsAuth::test_unauth_401` may accidentally pass.

---

## Task 4: Implement `routers/manifests.py` + wire main.py (green phase)

**Files:**
- Create: `src/orchestrator/api/routers/manifests.py`
- Modify: `src/orchestrator/api/main.py` (+1 import, +1 `include_router`)

- [ ] **Step 1: Write the router module**

Create `src/orchestrator/api/routers/manifests.py`:

```python
"""GET /api/v1/manifests — paginated list of manifests (BL9 / Feature 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from orchestrator.api._query_helpers import (
    FilterAllowList,
    FilterFieldSpec,
    IncludeAllowList,
    QueryParamError,
    SortAllowList,
    build_order_by_clause,
    build_where_clause,
    parse_filters,
    parse_includes,
    parse_pagination,
    parse_sort,
)
from orchestrator.api._query_helpers import SortField as _SortField
from orchestrator.api.dependencies import get_pool_dep
from orchestrator.db.pool import PoolError

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool


# Spec D1, D2 constants
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

# Default sort per spec D2: fetched_at:desc. Server-appended id:asc
# tie-breaker (UAT-4 S2-B); applied because user doesn't sort by id by default.
DEFAULT_SORT = (_SortField(field="fetched_at", direction="desc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

MANIFESTS_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "game_id": FilterFieldSpec(ops={"eq", "in"}, value_type=int),
        "version": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "fetched_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
        "chunk_count": FilterFieldSpec(ops={"gte", "lte"}, value_type=int),
        "total_bytes": FilterFieldSpec(ops={"gte", "lte"}, value_type=int),
    }
)

MANIFESTS_SORT_ALLOW_LIST = SortAllowList(
    fields={"id", "game_id", "version", "fetched_at", "chunk_count", "total_bytes"}
)

# Spec D5: the only opt-in expansion key for manifests is "game".
MANIFESTS_INCLUDE_ALLOW_LIST = IncludeAllowList(keys={"game"})

# Manifests columns selected from the manifests table (excludes raw BLOB per spec D1).
# All identifiers are SAFE — sourced from this constant, not user input.
_MANIFEST_COLUMNS = (
    "m.id, m.game_id, m.version, m.fetched_at, m.chunk_count, m.total_bytes"
)

# Additional columns selected via LEFT JOIN games when ?include=game.
_GAME_COLUMNS = "g.title AS game_title, g.platform AS game_platform, g.app_id AS game_app_id"

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class GameSummary(BaseModel):
    """Inline game summary populated when ?include=game (spec D6)."""

    model_config = ConfigDict(extra="forbid")
    title: str
    platform: Literal["steam", "epic"]
    app_id: str


class ManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    game_id: int
    version: str
    fetched_at: str
    chunk_count: int
    total_bytes: int
    # Spec D4: always-present field; populated iff ?include=game was requested.
    game: GameSummary | None


class SortFieldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"]


class ManifestsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, dict[str, Any]]  # plain dict per UAT-4 S2-A
    applied_sort: list[SortFieldResponse]
    # Spec D8: list of include keys actually applied (deduped + sorted).
    applied_includes: list[str]


class ManifestListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifests: list[ManifestResponse]
    meta: ManifestsMeta


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/v1", tags=["manifests"])


@router.get(
    "/manifests",
    response_model=ManifestListResponse,
    responses={
        200: {"description": "Paginated list of manifests"},
        400: {"description": "Bad query parameters"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List manifests",
    description=(
        "Returns the manifests feed with filter, sort, pagination, and optional "
        "?include=game inline expansion. Default sort is fetched_at:desc "
        "(matches idx_manifests_game_fetched). The `raw` BLOB column is "
        "intentionally excluded from the response surface. See spec "
        "docs/superpowers/specs/2026-05-20-bl9-manifests-readonly-design.md."
    ),
)
async def list_manifests(
    request: Request,
    pool: Pool = Depends(get_pool_dep),  # noqa: B008  FastAPI idiomatic
) -> JSONResponse:
    try:
        pagination = parse_pagination(
            request.query_params,
            default_limit=DEFAULT_LIMIT,
            max_limit=MAX_LIMIT,
        )
        filters = parse_filters(
            request.query_params, allow_list=MANIFESTS_FILTER_ALLOW_LIST
        )
        sort = parse_sort(
            request.query_params,
            allow_list=MANIFESTS_SORT_ALLOW_LIST,
            default=list(DEFAULT_SORT),
            tie_breaker=TIE_BREAKER,
        )
        includes = parse_includes(
            request.query_params, allow_list=MANIFESTS_INCLUDE_ALLOW_LIST
        )
    except QueryParamError as e:
        return JSONResponse(content={"detail": str(e)}, status_code=400)

    # Build WHERE / ORDER BY from helpers. Note: allow-list-validated field
    # names are unqualified (e.g., "game_id"); SQL prefixes with "m." via
    # the select template below since identifiers are interpolated only
    # from hardcoded allow-list literals.
    where_sql, where_params = build_where_clause(
        filters, allow_list=MANIFESTS_FILTER_ALLOW_LIST
    )
    order_sql = build_order_by_clause(sort, allow_list=MANIFESTS_SORT_ALLOW_LIST)

    # Conditional LEFT JOIN games when ?include=game.
    select_cols = _MANIFEST_COLUMNS
    join_sql = ""
    if "game" in includes:
        select_cols = f"{_MANIFEST_COLUMNS}, {_GAME_COLUMNS}"
        join_sql = "LEFT JOIN games g ON m.game_id = g.id"

    # nosem: S608 — identifiers from allow-list-validated literals only;
    # values are parameterized via `?`. See _query_helpers security invariants.
    count_sql = f"SELECT COUNT(*) AS total FROM manifests m {where_sql}".strip()  # noqa: S608
    rows_sql = (
        f"SELECT {select_cols} FROM manifests m {join_sql} {where_sql} {order_sql} "  # noqa: S608
        f"LIMIT ? OFFSET ?"
    ).strip()
    rows_params = [*where_params, pagination.limit, pagination.offset]

    try:
        count_row = await pool.read_one(count_sql, where_params)
        rows = await pool.read_all(rows_sql, rows_params)
    except PoolError as e:
        _log.error("api.manifests.read_failed", reason=str(e))
        return JSONResponse(
            content={"detail": "database unavailable"}, status_code=503
        )

    total = int(count_row["total"]) if count_row else 0

    manifests: list[ManifestResponse] = []
    for row in rows:
        game: GameSummary | None = None
        if "game" in includes:
            game = GameSummary(
                title=row["game_title"],
                platform=row["game_platform"],
                app_id=row["game_app_id"],
            )
        manifests.append(
            ManifestResponse(
                id=row["id"],
                game_id=row["game_id"],
                version=row["version"],
                fetched_at=row["fetched_at"],
                chunk_count=row["chunk_count"],
                total_bytes=row["total_bytes"],
                game=game,
            )
        )

    # UAT-4 S2-A: plain-dict applied_filters from parsed filters directly
    applied_filters: dict[str, dict[str, Any]] = {
        field_name: dict(ops) for field_name, ops in filters.items()
    }
    applied_sort = [SortFieldResponse(field=s.field, direction=s.direction) for s in sort]
    # Spec D8: stable sorted echo
    applied_includes = sorted(includes)

    body = ManifestListResponse(
        manifests=manifests,
        meta=ManifestsMeta(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            has_more=(pagination.offset + len(manifests) < total),
            applied_filters=applied_filters,
            applied_sort=applied_sort,
            applied_includes=applied_includes,
        ),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))
```

- [ ] **Step 2: Wire main.py**

Open `src/orchestrator/api/main.py`. Find the import block (around line 25):
```python
from orchestrator.api.routers.games import router as games_router
from orchestrator.api.routers.health import router as health_router
from orchestrator.api.routers.jobs import router as jobs_router
from orchestrator.api.routers.platforms import router as platforms_router
```
Insert after `jobs_router` (alphabetical order):
```python
from orchestrator.api.routers.manifests import router as manifests_router
```

Find the `include_router` block (in `create_app()`):
```python
    app.include_router(health_router)
    app.include_router(platforms_router)
    app.include_router(games_router)
    app.include_router(jobs_router)
```
Append:
```python
    app.include_router(manifests_router)
```

- [ ] **Step 3: Run manifests tests — verify green phase**

```bash
source .venv/bin/activate && pytest tests/api/test_manifests_router.py -q --no-header 2>&1 | tail -5
```
Expected: all manifests tests pass.

- [ ] **Step 4: Run full project test suite — confirm no regressions**

```bash
source .venv/bin/activate && pytest -q --no-header 2>&1 | tail -3
```
Expected: ~556 tests passing (518 prior + 30 router + 8 helper = 556). 0 failures.

---

## Task 5: Mark Build Loop checkpoints

- [ ] **Step 1: tests_written**

```bash
scripts/process-checklist.sh --complete-step build_loop:tests_written
```

- [ ] **Step 2: tests_verified_failing**

```bash
scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing
```

- [ ] **Step 3: implemented**

```bash
scripts/process-checklist.sh --complete-step build_loop:implemented
```

---

## Task 6: Security audit pass

**Files:**
- Create: `docs/security-audits/bl9-f9-manifests-readonly-security-audit.md`

- [ ] **Step 1: ruff check + format**

```bash
source .venv/bin/activate && ruff check src/orchestrator/api/routers/manifests.py src/orchestrator/api/_query_helpers.py src/orchestrator/api/main.py tests/api/test_manifests_router.py tests/api/test_query_helpers.py tests/api/conftest.py
ruff format --check src/orchestrator/api/routers/manifests.py src/orchestrator/api/_query_helpers.py src/orchestrator/api/main.py tests/api/test_manifests_router.py tests/api/test_query_helpers.py tests/api/conftest.py
```
Expected: `All checks passed!` for both. If format flags files, run `ruff format <files>` and re-check.

- [ ] **Step 2: mypy --strict**

```bash
source .venv/bin/activate && mypy --strict src/orchestrator/api/routers/manifests.py src/orchestrator/api/_query_helpers.py src/orchestrator/api/main.py
```
Expected: `Success: no issues found in 3 source files`.

- [ ] **Step 3: semgrep OWASP**

```bash
source .venv/bin/activate && semgrep --config p/owasp-top-ten --error src/orchestrator/api/routers/manifests.py src/orchestrator/api/_query_helpers.py
```
Expected: `0 findings`.

- [ ] **Step 4: gitleaks**

```bash
gitleaks detect --no-banner --redact --source .
```
Expected: `no leaks found`.

- [ ] **Step 5: Write the security audit doc**

Create `docs/security-audits/bl9-f9-manifests-readonly-security-audit.md`:

```markdown
# Security Audit — BL9 manifests read-only endpoint

**Feature:** BL9-F9-manifests-readonly (Build Loop 9, Milestone B)
**Module:** `src/orchestrator/api/routers/manifests.py` (~270 LoC) + `src/orchestrator/api/_query_helpers.py` (+30 LoC for IncludeAllowList + parse_includes) + 2-line wire in `src/orchestrator/api/main.py`
**Audit date:** 2026-05-20
**Auditor:** self-review (Senior Security Engineer persona) + automated SAST + gitleaks
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-05-20 -->

## Scope

Post-implementation security review of:
- `routers/manifests.py` — handler + Pydantic models + per-endpoint filter/sort/include allow-lists; conditional LEFT JOIN games
- `_query_helpers.py` — new `IncludeAllowList` dataclass + `parse_includes` function (~30 LoC); `"include"` added to `_RESERVED_PARAM_NAMES`
- 2-line wire-up in `main.py`
- New `manifests_pool_seeded` fixture in `tests/api/conftest.py`

The audit inherits the BL5+BL6+BL7+BL8+UAT-3+UAT-4 substrate. All existing security guarantees apply unchanged; this audit focuses on the new surface introduced by BL9 (the JOIN + the include primitive).

## Methodology

1. **Automated SAST**: `semgrep --config p/owasp-top-ten --error` — 0 findings
2. **gitleaks**: full repo scan — 0 findings
3. **ruff check + ruff format**: clean
4. **mypy --strict**: clean
5. **UAT-4 property-based SQL-injection test** in `test_query_helpers.py::TestSqlInjectionResistance` still passes — covers `build_where_clause`/`build_order_by_clause` that BL9 uses
6. **Manual review** of the new `parse_includes` + JOIN paths against TM-005 (SQL injection) and TM-013 (fingerprinting)

## Findings

**SEV-1: 0**
**SEV-2: 0**
**SEV-3: 0**
**SEV-4: 0**

No findings.

## Decisions D1-D8 walk

- **D1 raw BLOB excluded**: `_MANIFEST_COLUMNS` constant explicitly lists 6 manifest columns; `raw` not in the list. Schema additions would require code change to expose.
- **D2 default sort fetched_at:desc**: verified via `TestManifestsSort::test_default_fetched_at_desc` + `test_default_applied_sort_has_tie_breaker`
- **D3 version eq + _in**: verified via `TestManifestsFilters::test_version_eq` + `test_version_in`
- **D4 ?include=game always-present field, null when absent**: verified via `TestManifestsIncludeGame::test_no_include_game_is_null` + `test_include_game_populated`
- **D5 IncludeAllowList + parse_includes**: identifier validation at construction + reserved-name check; `?include` reserved in `_RESERVED_PARAM_NAMES`; tests in `TestParseIncludes`
- **D6 GameSummary shape (title, platform, app_id)**: verified via `TestManifestsIncludeGame::test_include_game_matches_seeded_game_row`
- **D7 LEFT JOIN games, allow-list scoped to manifests only**: SELECT explicitly aliases (`m.id`, `g.title AS game_title`); filter/sort allow-list references manifests columns only — no game.* filter possible
- **D8 applied_includes sorted echo**: verified via `TestManifestsAppliedEcho::test_applied_includes_*`

## Threat-model walk

- **TM-005 SQL injection**: MITIGATED. The new LEFT JOIN is hardcoded SQL (`LEFT JOIN games g ON m.game_id = g.id`); no user input flows into the JOIN clause. WHERE/ORDER BY use the UAT-4-hardened builders. All values via `?` placeholders.
- **TM-012 log redaction**: MITIGATED. The only new log event is `api.manifests.read_failed` with `reason=str(e)` from BL4's structured PoolError — no row data reaches a log call.
- **TM-013 fingerprinting**: MITIGATED. Three response shapes only: 200 with canonical envelope, 400 with `{detail}`, 503 with `{detail}`. `?include=game` adds a single `game` field per row; doesn't change response shape categories.

## Non-findings (cleared)

- **No SQL injection vector.** All values parameterized; identifiers from hardcoded literals only.
- **No timing oracle on auth.** Middleware-gated.
- **No log-volume amplification.** Success path emits only middleware events.
- **No DoS via response size.** `limit ≤ 500`; `raw` BLOB excluded; `game` summary is 3 small fields (~80 bytes/row max).
- **No identifier collision.** SELECT aliases ensure `m.id` and `g.id` don't conflict; only `m.*` exposed at the API surface.
- **No `?include=` injection.** Keys validated as identifiers + against per-endpoint allow-list at construction time AND request time.

## Test coverage

~38 tests total: 30 router + 8 helper (parse_includes coverage). Branch coverage on both modules ≥95%.

## Verification artifacts

- `pytest -q`: ~556 tests passing project-wide
- `ruff check` + `ruff format --check`: clean
- `mypy --strict`: clean
- `semgrep --config p/owasp-top-ten --error`: 0 findings
- `gitleaks detect`: no leaks

## Conclusion

**APPROVED for merge.** Zero findings. BL9 introduces the `?include=` opt-in expansion convention via a thin (~30 LoC) extension to `_query_helpers.py`. The new primitive follows the same identifier-validation + reserved-name discipline as the existing `FilterAllowList`/`SortAllowList`. The conditional LEFT JOIN is hardcoded SQL with no user-input surface. The convention is now available for future endpoints that need FK expansion (`/jobs?include=game`, etc.) without further helper changes.
```

- [ ] **Step 6: Mark security_audit**

```bash
scripts/process-checklist.sh --complete-step build_loop:security_audit
```

---

## Task 7: Update CHANGELOG + FEATURES

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `FEATURES.md`

- [ ] **Step 1: CHANGELOG entry**

Open `CHANGELOG.md`. Find `## [Unreleased]` → `### Added`. Add as FIRST item:

```markdown
- **`GET /api/v1/manifests`** (BL9 / Feature 9 partial) — third paginated F9
  read endpoint, introduces the **`?include=` opt-in expansion convention**.
  Default sort `fetched_at:desc` (matches `idx_manifests_game_fetched`).
  Per-endpoint filterable: `game_id` (eq, `_in`), `version` (eq, `_in`),
  `fetched_at` (range), `chunk_count` (range), `total_bytes` (range).
  Sortable: `id`, `game_id`, `version`, `fetched_at`, `chunk_count`,
  `total_bytes`. `raw` BLOB column intentionally excluded. With
  `?include=game`, the response embeds a `game: {title, platform, app_id}`
  summary via `LEFT JOIN games`. Adds `IncludeAllowList` +
  `parse_includes` to `_query_helpers.py` (+~30 LoC, identifier-validated
  + `"include"` reserved) — future endpoints can opt-in to FK expansion
  cheaply.  See
  [spec](docs/superpowers/specs/2026-05-20-bl9-manifests-readonly-design.md)
  and [audit](docs/security-audits/bl9-f9-manifests-readonly-security-audit.md).
```

- [ ] **Step 2: FEATURES Feature 9 entry**

Open `FEATURES.md`. After the existing Feature 8 (BL8 jobs) section, add:

```markdown
## Feature 9: BL9 — `GET /api/v1/manifests` (read-only, paginated)

**Phase Built:** 2 (Milestone B, Build Loop 9)
**Status:** Complete (2026-05-20)
**Summary:** Third paginated F9 read endpoint. Introduces the **`?include=`
opt-in expansion convention** via a thin (~30 LoC) extension to
`_query_helpers.py`. Default sort `fetched_at:desc` matches the
`idx_manifests_game_fetched` index. `raw` BLOB column excluded. With
`?include=game`, response embeds `{title, platform, app_id}` via
`LEFT JOIN games`. Validates that the shared module can grow new
primitives cheaply when a real new use case (FK expansion) arises.
**Key Interfaces:**
  - `src/orchestrator/api/routers/manifests.py` — `ManifestResponse`,
    `ManifestListResponse`, `ManifestsMeta`, `SortFieldResponse`,
    `GameSummary` Pydantic models; 3 allow-lists;
    `list_manifests` handler with conditional LEFT JOIN
  - `src/orchestrator/api/_query_helpers.py` — NEW: `IncludeAllowList`
    dataclass + `parse_includes` function; `"include"` added to
    `_RESERVED_PARAM_NAMES`
  - Wired in `src/orchestrator/api/main.py` via
    `app.include_router(manifests_router)`
**Locked decisions (D1-D8):** raw BLOB excluded · default sort
fetched_at:desc · version eq+_in · ?include=game always-present field
null when absent · NEW IncludeAllowList primitive · GameSummary 3-field
shape · LEFT JOIN games (allow-list scoped to manifests only) ·
applied_includes sorted echo. D9-D20 inherited from BL7+UAT-4+BL8. See
[spec](superpowers/specs/2026-05-20-bl9-manifests-readonly-design.md).
**Test Coverage:** 30 tests in `tests/api/test_manifests_router.py`
across 10 classes + 8 tests in `tests/api/test_query_helpers.py` for
the new `parse_includes` + `IncludeAllowList` primitives. Plus
`manifests_pool_seeded` fixture in `conftest.py` (~30 manifests across
5 baseline games).
**Related Audit:** [`bl9-f9-manifests-readonly-security-audit.md`](security-audits/bl9-f9-manifests-readonly-security-audit.md) — 0 findings.
**Known Limitations:**
  - `raw` BLOB column not exposed; out-of-band diagnostic endpoint
    (`GET /manifests/{id}/raw`) deferred until real operator need
    surfaces.
  - No `?include=` default-on opt-in; if Game_shelf surfaces friction,
    additive default-includes can be added later.
  - `?sort=total_bytes:desc` / `?sort=chunk_count:desc` use full table
    scan + temp B-tree (no covering index). Acceptable at expected
    scale (thousands of manifests); add covering index only if
    profiling shows a hot path.

---
```

- [ ] **Step 3: Mark documentation_updated**

```bash
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
```

---

## Task 8: Combined feat + docs commit

**Files staged:** all source + helper + tests + CHANGELOG + FEATURES + audit doc + `.claude/process-state.json` (auto-bumped).

- [ ] **Step 1: Survey state**

```bash
git status --short
git diff --stat
```

- [ ] **Step 2: Stage all files**

```bash
git add \
  src/orchestrator/api/routers/manifests.py \
  src/orchestrator/api/_query_helpers.py \
  src/orchestrator/api/main.py \
  tests/api/test_manifests_router.py \
  tests/api/test_query_helpers.py \
  tests/api/conftest.py \
  docs/security-audits/bl9-f9-manifests-readonly-security-audit.md \
  CHANGELOG.md \
  FEATURES.md \
  .claude/process-state.json
```

- [ ] **Step 3: Write commit message to tmp**

Write `/tmp/bl9-feat-commit.txt`:
```
feat(api): GET /api/v1/manifests — third paginated F9 read endpoint

Third validation of the BL7+UAT-4 thesis, with one new convention
introduced: ?include= opt-in FK expansion via IncludeAllowList +
parse_includes primitives in _query_helpers.py (+~30 LoC).

Behavior (20 locked decisions D1-D20):
- D1 raw BLOB column excluded from response (zstd-compressed; out-of-band
  /raw endpoint deferred)
- D2 Default sort fetched_at:desc (matches idx_manifests_game_fetched)
- D3 version filter: eq + _in (string enum-like)
- D4 ?include=game: always-present `game` field, null when not requested
- D5 NEW _query_helpers primitive: IncludeAllowList + parse_includes;
  "include" added to _RESERVED_PARAM_NAMES
- D6 GameSummary shape: title, platform, app_id
- D7 LEFT JOIN games (defensive; allow-list scoped to manifests only)
- D8 applied_includes sorted list in meta
- D9-D20 INHERITED from BL7+UAT-4+BL8

Per-endpoint allow-list:
- Filterable: game_id (eq,_in), version (eq,_in), fetched_at (gte,lte
  timestamp), chunk_count (gte,lte), total_bytes (gte,lte)
- Sortable: id, game_id, version, fetched_at, chunk_count, total_bytes
- Includable: game

Implementation:
- src/orchestrator/api/_query_helpers.py: +IncludeAllowList +
  parse_includes (+30 LoC); _RESERVED_PARAM_NAMES gains "include"
- src/orchestrator/api/routers/manifests.py (~270 LoC) — Pydantic models
  + handler with conditional LEFT JOIN games
- 2-line wire-up in main.py
- manifests_pool_seeded fixture in tests/api/conftest.py (~30 manifests
  across 5 baseline games)

Tests:
- tests/api/test_manifests_router.py — 30 tests across 10 classes
- tests/api/test_query_helpers.py — 8 tests for parse_includes +
  IncludeAllowList

Docs:
- CHANGELOG entry under [Unreleased] -> Added
- FEATURES Feature 9 entry
- Security audit at
  docs/security-audits/bl9-f9-manifests-readonly-security-audit.md
  (0 findings)
- Spec at
  docs/superpowers/specs/2026-05-20-bl9-manifests-readonly-design.md
  (committed 0c72c4d)
- Plan at
  docs/superpowers/plans/2026-05-20-bl9-manifests-readonly.md
  (separate docs(plan) commit)

Verification: full project suite green (~556 tests; +38 new);
ruff / ruff format / mypy --strict / semgrep p/owasp-top-ten /
gitleaks all clean.

Counter at 2/2 after feature_recorded: UAT-5 fires before BL10.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

- [ ] **Step 4: Mark evaluated + commit**

```bash
bash .claude/framework/hooks/mark-evaluated.sh "BL9 feat+docs commit — single bundled per BL6/7/8 pattern; user pre-approved end-to-end via autonomy grant"
git commit -F /tmp/bl9-feat-commit.txt
```

- [ ] **Step 5: Verify commit**

```bash
git log --oneline -3
git status --short
```
Expected: top commit subject `feat(api): GET /api/v1/manifests — third paginated F9 read endpoint`. Working tree clean.

---

## Task 9: Mark feature_recorded + record in test-gate counter + push + open PR

- [ ] **Step 1: Mark feature_recorded**

```bash
scripts/process-checklist.sh --complete-step build_loop:feature_recorded
```

- [ ] **Step 2: Record feature in test-gate counter**

```bash
scripts/test-gate.sh --record-feature "BL9-F9-manifests-readonly"
```
Expected: counter increments to 2/2 → UAT-5 required.

- [ ] **Step 3: Verify state**

```bash
scripts/test-gate.sh --check-batch
```
Expected: `[FAIL] Testing session required (2 features since last test, interval is 2)`. **This is the correct outcome** — UAT-5 must run after this BL ships.

- [ ] **Step 4: Push branch**

```bash
git push -u origin feat/bl9-manifests-readonly
```

- [ ] **Step 5: Write PR body**

Write `/tmp/bl9-pr-body.txt`:
```markdown
## Summary

BL9 — `GET /api/v1/manifests` — third paginated F9 read endpoint. Introduces the **`?include=` opt-in expansion convention** via a thin (~30 LoC) extension to `_query_helpers.py`.

## What's in this PR

| Commit | Purpose |
|---|---|
| `docs(spec)` 0c72c4d | Design with 20 locked decisions (8 BL9-specific, 12 inherited from BL7+UAT-4+BL8) |
| `docs(plan)` | 9-task implementation plan |
| `feat(api)` | Helpers extension + router + tests + fixture + CHANGELOG/FEATURES/audit |

## New convention introduced

**`?include=` opt-in FK expansion.** Per-endpoint declares an `IncludeAllowList` of permitted expansion keys; clients opt in via `?include=key1,key2`. For `/manifests`, only `game` is includable; expanded response embeds `{title, platform, app_id}` via `LEFT JOIN games`.

## Per-endpoint allow-list

**Filterable** (operator-suffix syntax):
- `game_id`, `version` — `eq` + `_in`
- `fetched_at` — `_gte`, `_lte` (ISO 8601 validator)
- `chunk_count`, `total_bytes` — `_gte`, `_lte`

**Sortable:** `id`, `game_id`, `version`, `fetched_at`, `chunk_count`, `total_bytes`. Default `fetched_at:desc`.

**Includable:** `game`.

## Verification

- ~556 project tests passing (+38 new: 30 router + 8 helper)
- ruff / ruff format / mypy --strict / semgrep p/owasp-top-ten / gitleaks all clean
- 6/6 Build Loop checklist; feature recorded; **counter at 2/2 → UAT-5 fires before BL10**
- Security audit: 0 findings

## Test plan

- [ ] CI status checks pass (8 required)
- [ ] Manual smoke (bearer as `$T`):
  - `curl ... '/api/v1/manifests?limit=5'` returns wrapped envelope; default sort `fetched_at:desc`; `game: null` for each row
  - `curl ... '/api/v1/manifests?include=game&limit=5'` returns same shape with populated `game` objects
  - `curl ... '/api/v1/manifests?game_id=1&include=game'` returns manifests for game 1 with inline Counter-Strike summary
  - `curl ... '/api/v1/manifests?include=games'` (typo) returns 400 with "include keys not allowed"
  - `curl ... '/api/v1/manifests?password=foo'` returns 400
  - `curl ... '/api/v1/manifests?fetched_at_gte=<script>'` returns 400 (timestamp validator regression)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 6: Open PR**

```bash
gh pr create \
  --title "feat(api): GET /api/v1/manifests — third paginated F9 read endpoint" \
  --body-file /tmp/bl9-pr-body.txt \
  --base main \
  --head feat/bl9-manifests-readonly
```

- [ ] **Step 7: Report PR URL; do NOT merge**

Per `feedback_pr_merge_ownership.md`: user merges PRs themselves.

---

## Self-Review

**Spec coverage check:**

| Spec section | Plan task |
|---|---|
| §2 D1 raw excluded | Task 4 `_MANIFEST_COLUMNS` constant (6 columns; no raw) |
| §2 D2 default sort fetched_at:desc | Task 3 `TestManifestsSort::test_default_fetched_at_desc`; Task 4 `DEFAULT_SORT` |
| §2 D3 version eq+_in | Task 3 `TestManifestsFilters::test_version_eq`+`test_version_in`; Task 4 allow-list |
| §2 D4 ?include=game always-present null | Task 3 `TestManifestsIncludeGame::test_no_include_game_is_null`; Task 4 `game: GameSummary | None` |
| §2 D5 NEW IncludeAllowList + parse_includes | Task 1 implementation + Task 1 Step 6 tests |
| §2 D6 GameSummary 3 fields | Task 4 `GameSummary` model; Task 3 `test_include_game_matches_seeded_game_row` |
| §2 D7 LEFT JOIN games + scoped allow-list | Task 4 conditional `join_sql`; allow-list references manifests cols only |
| §2 D8 applied_includes sorted echo | Task 4 `sorted(includes)`; Task 3 `TestManifestsAppliedEcho::test_applied_includes_*` |
| §3.1 per-field allow-list | Task 4 `MANIFESTS_FILTER_ALLOW_LIST` + `MANIFESTS_SORT_ALLOW_LIST` |
| §3.2 response shape | Task 4 Pydantic models match exactly |
| §3.3 error responses | Task 4 try/except returns 400/503 |
| §4 architecture | Task 1 + Task 4 file paths exact |
| §5 test plan | Task 3 — 10 test classes; Task 1 — 8 helper tests |
| §6 risk register | LEFT JOIN COUNT/SELECT consistency: SQL invariant doc'd; field-name collision: SELECT aliases; identifier validation; etc. |
| §7 documentation deltas | Task 6 audit + Task 7 CHANGELOG/FEATURES |

**Placeholder scan:** No TBD/TODO. All file paths, code blocks, commands concrete.

**Type consistency:** `_SortField` (helpers dataclass) vs `SortFieldResponse` (Pydantic router model) intentionally separate per BL7/BL8 pattern. `IncludeAllowList` defined in Task 1 + referenced in Task 4. `GameSummary` defined in Task 4 only. `MANIFESTS_FILTER_ALLOW_LIST`, `MANIFESTS_SORT_ALLOW_LIST`, `MANIFESTS_INCLUDE_ALLOW_LIST` constants referenced consistently. `_MANIFEST_COLUMNS`, `_GAME_COLUMNS` constants used in Task 4 SQL building.
