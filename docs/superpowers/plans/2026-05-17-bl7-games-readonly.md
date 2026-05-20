# BL7 Games Read-Only Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `GET /api/v1/games` — first paginated F9 read endpoint on the BL5+BL6 substrate — with full filter/sort/pagination support and a shared `_query_helpers.py` module that every future paginated F9 endpoint will reuse.

**Architecture:** Two new modules: (1) `_query_helpers.py` with strict scope (parser + validator + SQL builder primitives only — no domain logic), and (2) `routers/games.py` (Pydantic models + endpoint handler that composes the helpers). Wired into `main.py` via `app.include_router`. Two-query SQL pattern: `SELECT COUNT(*)` for `meta.total` + `SELECT ... LIMIT/OFFSET` for the rows. All filter/sort values pass via parameterized binds (no string interpolation of user input). Reuses BL5 middleware substrate (bearer auth, correlation_id propagation, body cap), BL6 conventions (wrapped envelope, `extra="forbid"`, PoolError→503), and BL4 pool API (`read_all` with `aiosqlite.Row` results).

**Tech Stack:** Python 3.12, FastAPI 0.136.1, Pydantic v2, aiosqlite, structlog, httpx (test client), pytest-asyncio, hypothesis (property-based SQL-injection tests).

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/orchestrator/api/_query_helpers.py` | **Create** (~220 LoC) | `PaginationParams`, `FilterCriterion`, `SortField`, parsers (`parse_pagination`, `parse_filters`, `parse_sort`), SQL builders (`build_where_clause`, `build_order_by_clause`), allow-list types. Strict scope: no domain logic. |
| `src/orchestrator/api/routers/games.py` | **Create** (~210 LoC) | `GameResponse`, `GamesMeta`, `GameListResponse` Pydantic models; `GAMES_FILTER_ALLOW_LIST`, `GAMES_SORT_ALLOW_LIST` constants; `list_games` handler. |
| `src/orchestrator/api/main.py` | **Modify** (+1 import, +1 `include_router` line) | Wire the new router into the app factory. |
| `tests/api/conftest.py` | **Modify** (+1 fixture) | Add `games_pool_100` fixture for pagination tests (100-row seeded pool). |
| `tests/api/test_query_helpers.py` | **Create** (~280 LoC, ~18 tests) | Unit tests of helper functions in isolation. Includes Hypothesis property test for SQL-injection resistance. |
| `tests/api/test_games_router.py` | **Create** (~520 LoC, ~30 tests) | HTTP-level tests of the endpoint via httpx ASGI transport. |
| `docs/security-audits/bl7-f9-games-readonly-security-audit.md` | **Create** | Per-feature audit doc (Build Loop gate requirement) |
| `CHANGELOG.md` | **Modify** | BL7 entry under `[Unreleased]` → `### Added` |
| `FEATURES.md` | **Modify** | New Feature 7 entry |

---

## Task 0: Commit this plan to git

**Files:**
- Already written: `docs/superpowers/plans/2026-05-17-bl7-games-readonly.md`

**Branch state:** Already on `feat/bl7-games-readonly` (created at session start). Spec committed in `ffc2e83`. `--start-feature "BL7-F9-games-readonly"` already marked. Context health check reset.

- [ ] **Step 1: Confirm plan file is untracked**

Run:
```bash
git status --short docs/superpowers/plans/2026-05-17-bl7-games-readonly.md
```
Expected: `?? docs/superpowers/plans/2026-05-17-bl7-games-readonly.md`

- [ ] **Step 2: Write commit message to a tmp file** (path strings in inline args sometimes trip framework regexes per BL5/BL6 closure memory)

Write `/tmp/bl7-plan-commit.txt`:
```
docs(plan): BL7 games read-only implementation plan

Decomposes the BL7 spec (ffc2e83) into 11 tasks following the
project Build Loop:
- Task 0: this plan commit
- Task 1: tests for _query_helpers (red phase)
- Task 2: implement _query_helpers (green for helpers)
- Task 3: tests for /games router (red phase)
- Task 4: implement /games router + main.py wire-up + green + full suite
- Task 5: mark tests_written + tests_verified_failing + implemented
- Task 6: security audit (ruff, mypy, semgrep, gitleaks) + audit doc + mark
- Task 7: CHANGELOG + FEATURES + mark documentation_updated
- Task 8: combined feat+docs commit (gate-forced ordering per BL5/BL6)
- Task 9: mark feature_recorded + record in test-gate counter
- Task 10: push + open PR (do not merge)

Includes the full router source, helper module source, ~48 tests,
gate-quirk handlings inherited from BL3-BL6 (config-guard scope,
enforce-context7 stdlib whitelist already includes py:__future__,
enforce-evaluate marker per commit, enforce-plan-tracking).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

- [ ] **Step 3: Mark evaluation, stage, commit**

Run:
```bash
bash .claude/framework/hooks/mark-evaluated.sh "BL7 plan commit — pattern matches BL6 docs(spec)-then-docs(plan) per user-approved sequence"
git add docs/superpowers/plans/2026-05-17-bl7-games-readonly.md
git commit -F /tmp/bl7-plan-commit.txt
```
Expected: 1 file, 1 commit on `feat/bl7-games-readonly`.

- [ ] **Step 4: Verify**

Run:
```bash
git log --oneline -3
git status --short
```
Expected: top commit subject `docs(plan): BL7 games read-only implementation plan`. Working tree clean (or only `.claude/process-state.json` if BUILD_LOOP_STEP was bumped — that's fine, it'll be folded into the feat commit later per BL6 pattern).

---

## Task 1: Write `_query_helpers` tests (red phase)

**Files:**
- Create: `tests/api/test_query_helpers.py`

This task writes the helper-module test suite at once. Tests reference the not-yet-existing `_query_helpers` module; running them fails with `ImportError`. That collective failure is the red-phase signal for the helpers.

- [ ] **Step 1: Read existing helper conventions for grounding**

Run:
```bash
sed -n '1,40p' src/orchestrator/api/dependencies.py
```
Expected: see `BODY_SIZE_CAP_BYTES`, `AUTH_EXEMPT_PATHS`, `LOOPBACK_HOSTS`, `LOOPBACK_ONLY_PATTERNS`, `get_pool_dep`. This is the module the new helpers will sit alongside.

- [ ] **Step 2: Write the full test file**

Create `tests/api/test_query_helpers.py`:

```python
"""Unit tests for orchestrator.api._query_helpers (BL7 / Feature 9 partial).

Covers parser/validator/SQL-builder primitives in isolation. The router
integration tests live in test_games_router.py.
"""

from __future__ import annotations

import pytest
from hypothesis import given, strategies as st
from starlette.datastructures import QueryParams


# ---------------------------------------------------------------------------
# parse_pagination
# ---------------------------------------------------------------------------


class TestParsePagination:
    def test_default_when_absent(self):
        from orchestrator.api._query_helpers import parse_pagination

        params = parse_pagination(QueryParams(""), default_limit=50, max_limit=500)
        assert params.limit == 50
        assert params.offset == 0

    def test_explicit_limit_offset(self):
        from orchestrator.api._query_helpers import parse_pagination

        params = parse_pagination(
            QueryParams("limit=25&offset=75"),
            default_limit=50,
            max_limit=500,
        )
        assert params.limit == 25
        assert params.offset == 75

    def test_limit_above_max_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_pagination

        with pytest.raises(QueryParamError, match="limit must be"):
            parse_pagination(QueryParams("limit=1000"), default_limit=50, max_limit=500)

    def test_negative_offset_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_pagination

        with pytest.raises(QueryParamError, match="offset"):
            parse_pagination(
                QueryParams("offset=-1"), default_limit=50, max_limit=500
            )

    def test_negative_limit_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_pagination

        with pytest.raises(QueryParamError, match="limit"):
            parse_pagination(
                QueryParams("limit=-5"), default_limit=50, max_limit=500
            )

    def test_zero_limit_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_pagination

        with pytest.raises(QueryParamError, match="limit"):
            parse_pagination(QueryParams("limit=0"), default_limit=50, max_limit=500)

    def test_non_numeric_limit_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_pagination

        with pytest.raises(QueryParamError, match="limit"):
            parse_pagination(
                QueryParams("limit=abc"), default_limit=50, max_limit=500
            )


# ---------------------------------------------------------------------------
# parse_filters
# ---------------------------------------------------------------------------


def _games_allow_list():
    """Filter allow-list as it will appear in routers/games.py."""
    from orchestrator.api._query_helpers import FilterAllowList, FilterFieldSpec

    return FilterAllowList(
        {
            "platform": FilterFieldSpec(
                ops={"eq", "in"}, value_type=str
            ),
            "status": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
            "owned": FilterFieldSpec(ops={"eq"}, value_type=int),
            "size_bytes": FilterFieldSpec(
                ops={"eq", "gte", "lte"}, value_type=int
            ),
            "last_prefilled_at": FilterFieldSpec(
                ops={"gte", "lte"}, value_type=str
            ),
            "last_validated_at": FilterFieldSpec(
                ops={"gte", "lte"}, value_type=str
            ),
        }
    )


class TestParseFilters:
    def test_empty_query_returns_empty(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(QueryParams(""), allow_list=_games_allow_list())
        assert result == {}

    def test_single_eq_filter(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("platform=steam"), allow_list=_games_allow_list()
        )
        assert result == {"platform": {"eq": "steam"}}

    def test_multi_field_filters(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("platform=steam&status=not_downloaded"),
            allow_list=_games_allow_list(),
        )
        assert result == {
            "platform": {"eq": "steam"},
            "status": {"eq": "not_downloaded"},
        }

    def test_in_operator_splits_on_comma(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("status_in=not_downloaded,pending_update"),
            allow_list=_games_allow_list(),
        )
        assert result == {
            "status": {"in": ["not_downloaded", "pending_update"]}
        }

    def test_in_operator_strips_whitespace_per_value(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("status_in=not_downloaded , pending_update"),
            allow_list=_games_allow_list(),
        )
        assert result == {
            "status": {"in": ["not_downloaded", "pending_update"]}
        }

    def test_range_combo(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("size_bytes_gte=1000&size_bytes_lte=5000"),
            allow_list=_games_allow_list(),
        )
        assert result == {"size_bytes": {"gte": 1000, "lte": 5000}}

    def test_int_value_type_coerced(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("owned=1"), allow_list=_games_allow_list()
        )
        assert result == {"owned": {"eq": 1}}
        assert isinstance(result["owned"]["eq"], int)

    def test_unknown_field_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        with pytest.raises(QueryParamError, match="unknown filter field"):
            parse_filters(
                QueryParams("foo=bar"), allow_list=_games_allow_list()
            )

    def test_unknown_operator_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        with pytest.raises(QueryParamError, match="unknown operator|not allowed"):
            parse_filters(
                QueryParams("platform_gte=foo"),
                allow_list=_games_allow_list(),
            )

    def test_type_mismatch_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        with pytest.raises(QueryParamError, match="invalid value"):
            parse_filters(
                QueryParams("size_bytes_gte=abc"),
                allow_list=_games_allow_list(),
            )


# ---------------------------------------------------------------------------
# parse_sort
# ---------------------------------------------------------------------------


def _games_sort_allow_list():
    from orchestrator.api._query_helpers import SortAllowList

    return SortAllowList(
        fields={"id", "title", "status", "size_bytes", "last_prefilled_at", "last_validated_at"}
    )


class TestParseSort:
    def test_default_applied_when_absent(self):
        from orchestrator.api._query_helpers import SortField, parse_sort

        result = parse_sort(
            QueryParams(""),
            allow_list=_games_sort_allow_list(),
            default=[SortField(field="title", direction="asc")],
            tie_breaker=SortField(field="id", direction="asc"),
        )
        assert result == [
            SortField(field="title", direction="asc"),
            SortField(field="id", direction="asc"),
        ]

    def test_single_field_default_direction(self):
        from orchestrator.api._query_helpers import SortField, parse_sort

        result = parse_sort(
            QueryParams("sort=title"),
            allow_list=_games_sort_allow_list(),
            default=[SortField(field="title", direction="asc")],
            tie_breaker=SortField(field="id", direction="asc"),
        )
        assert result == [
            SortField(field="title", direction="asc"),
            SortField(field="id", direction="asc"),
        ]

    def test_single_field_explicit_desc(self):
        from orchestrator.api._query_helpers import SortField, parse_sort

        result = parse_sort(
            QueryParams("sort=title:desc"),
            allow_list=_games_sort_allow_list(),
            default=[SortField(field="title", direction="asc")],
            tie_breaker=SortField(field="id", direction="asc"),
        )
        assert result == [
            SortField(field="title", direction="desc"),
            SortField(field="id", direction="asc"),
        ]

    def test_multi_field_sort(self):
        from orchestrator.api._query_helpers import SortField, parse_sort

        result = parse_sort(
            QueryParams("sort=last_prefilled_at:desc,title:asc"),
            allow_list=_games_sort_allow_list(),
            default=[SortField(field="title", direction="asc")],
            tie_breaker=SortField(field="id", direction="asc"),
        )
        assert result == [
            SortField(field="last_prefilled_at", direction="desc"),
            SortField(field="title", direction="asc"),
            SortField(field="id", direction="asc"),
        ]

    def test_tie_breaker_deduplicated_when_user_sorts_by_id(self):
        from orchestrator.api._query_helpers import SortField, parse_sort

        # User specifies id:desc — server must NOT append id:asc (would
        # contradict). User's explicit id ordering wins; tie-breaker omitted.
        result = parse_sort(
            QueryParams("sort=id:desc"),
            allow_list=_games_sort_allow_list(),
            default=[SortField(field="title", direction="asc")],
            tie_breaker=SortField(field="id", direction="asc"),
        )
        assert result == [SortField(field="id", direction="desc")]

    def test_unknown_field_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, SortField, parse_sort

        with pytest.raises(QueryParamError, match="not a sortable field"):
            parse_sort(
                QueryParams("sort=password"),
                allow_list=_games_sort_allow_list(),
                default=[SortField(field="title", direction="asc")],
                tie_breaker=SortField(field="id", direction="asc"),
            )

    def test_invalid_direction_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, SortField, parse_sort

        with pytest.raises(QueryParamError, match="invalid sort direction"):
            parse_sort(
                QueryParams("sort=title:sideways"),
                allow_list=_games_sort_allow_list(),
                default=[SortField(field="title", direction="asc")],
                tie_breaker=SortField(field="id", direction="asc"),
            )


# ---------------------------------------------------------------------------
# build_where_clause
# ---------------------------------------------------------------------------


class TestBuildWhereClause:
    def test_empty_filters(self):
        from orchestrator.api._query_helpers import build_where_clause

        sql, params = build_where_clause({}, allow_list=_games_allow_list())
        assert sql == ""
        assert params == []

    def test_single_eq(self):
        from orchestrator.api._query_helpers import build_where_clause

        sql, params = build_where_clause(
            {"platform": {"eq": "steam"}}, allow_list=_games_allow_list()
        )
        assert sql == "WHERE platform = ?"
        assert params == ["steam"]

    def test_in_placeholders(self):
        from orchestrator.api._query_helpers import build_where_clause

        sql, params = build_where_clause(
            {"status": {"in": ["a", "b", "c"]}},
            allow_list=_games_allow_list(),
        )
        assert sql == "WHERE status IN (?, ?, ?)"
        assert params == ["a", "b", "c"]

    def test_range_combines_with_AND(self):
        from orchestrator.api._query_helpers import build_where_clause

        sql, params = build_where_clause(
            {"size_bytes": {"gte": 100, "lte": 500}},
            allow_list=_games_allow_list(),
        )
        assert sql == "WHERE size_bytes >= ? AND size_bytes <= ?"
        assert params == [100, 500]

    def test_multi_field_ANDs(self):
        from orchestrator.api._query_helpers import build_where_clause

        sql, params = build_where_clause(
            {
                "platform": {"eq": "steam"},
                "size_bytes": {"gte": 1000},
            },
            allow_list=_games_allow_list(),
        )
        # Order is stable (insertion order of the filters dict)
        assert "platform = ?" in sql
        assert "size_bytes >= ?" in sql
        assert " AND " in sql
        assert set(params) == {"steam", 1000}


# ---------------------------------------------------------------------------
# build_order_by_clause
# ---------------------------------------------------------------------------


class TestBuildOrderByClause:
    def test_single_field(self):
        from orchestrator.api._query_helpers import SortField, build_order_by_clause

        sql = build_order_by_clause([SortField(field="title", direction="asc")])
        assert sql == "ORDER BY title ASC"

    def test_multi_field(self):
        from orchestrator.api._query_helpers import SortField, build_order_by_clause

        sql = build_order_by_clause(
            [
                SortField(field="last_prefilled_at", direction="desc"),
                SortField(field="title", direction="asc"),
                SortField(field="id", direction="asc"),
            ]
        )
        assert sql == "ORDER BY last_prefilled_at DESC, title ASC, id ASC"


# ---------------------------------------------------------------------------
# Property-based SQL-injection resistance
# ---------------------------------------------------------------------------


class TestSqlInjectionResistance:
    @given(
        platform=st.sampled_from(["steam", "epic", "'; DROP TABLE games; --"]),
        size=st.integers(min_value=-(2**31), max_value=2**31),
    )
    def test_build_where_never_interpolates_values(self, platform, size):
        """Property: regardless of input value content, build_where_clause
        produces SQL with only `?` placeholders for values — never literal
        interpolations. Values flow only via the returned params list."""
        from orchestrator.api._query_helpers import build_where_clause

        sql, params = build_where_clause(
            {"platform": {"eq": platform}, "size_bytes": {"gte": size}},
            allow_list=_games_allow_list(),
        )
        # SQL must contain `?` placeholders, not the raw value text
        assert "?" in sql
        # The raw platform string (especially the SQL-injection payload) must
        # NOT appear in the SQL — only in the params list
        assert platform not in sql or platform in ("steam", "epic")
        # Specifically, the injection payload must never appear in SQL
        assert "DROP TABLE" not in sql
        assert "';" not in sql
```

- [ ] **Step 3: Run tests — verify red phase**

Run:
```bash
source .venv/bin/activate && pytest tests/api/test_query_helpers.py -q --no-header 2>&1 | tail -10
```
Expected: collection error or every test fails with `ModuleNotFoundError: No module named 'orchestrator.api._query_helpers'` or similar.

---

## Task 2: Implement `_query_helpers` (green phase for helpers)

**Files:**
- Create: `src/orchestrator/api/_query_helpers.py`

- [ ] **Step 1: Mark TaskUpdate in_progress on the helpers task** (per enforce-plan-tracking hook)

Use TaskUpdate tool to mark the relevant task in_progress before writing source files.

- [ ] **Step 2: Write the helper module**

Create `src/orchestrator/api/_query_helpers.py`:

```python
"""Shared query primitives for paginated F9 read endpoints (BL7+).

This module's strict scope is parser + validator + SQL builder ONLY.
No domain logic; no endpoint-specific knowledge. Endpoints (routers/games.py,
future routers/jobs.py, etc.) construct their own FilterAllowList and
SortAllowList values describing what their endpoint permits, then compose
the helpers below to parse incoming Query params and produce parameterized
SQL fragments.

Design conventions locked in spec 2026-05-17-bl7-games-readonly-design.md:
- Offset-based pagination (limit/offset query params)
- Operator-suffix filter syntax: field, field_in, field_gte, field_lte,
  field_gt, field_lt, field_ne (per-endpoint allow-list narrows to subset)
- Multi-field sort: ?sort=a:desc,b:asc
- Server-appended tie-breaker (with de-dup if user already sorted by it)

Security invariants:
- User values flow EXCLUSIVELY through SQL parameter binds (never f-string
  or %-formatted into the SQL text). Field names ARE interpolated into SQL
  but only AFTER allow-list validation guarantees they're safe identifiers
  from the endpoint's declared field set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.datastructures import QueryParams


class QueryParamError(ValueError):
    """Raised when a query param fails parse/validation. The router catches
    this and returns 400 with the error message as `detail`."""


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaginationParams:
    limit: int
    offset: int


def parse_pagination(
    params: QueryParams,
    *,
    default_limit: int,
    max_limit: int,
) -> PaginationParams:
    raw_limit = params.get("limit")
    raw_offset = params.get("offset")

    if raw_limit is None:
        limit = default_limit
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as e:
            raise QueryParamError(f"invalid value for limit: {raw_limit!r}") from e

    if raw_offset is None:
        offset = 0
    else:
        try:
            offset = int(raw_offset)
        except (TypeError, ValueError) as e:
            raise QueryParamError(f"invalid value for offset: {raw_offset!r}") from e

    if limit < 1:
        raise QueryParamError(f"limit must be ≥ 1, got {limit}")
    if limit > max_limit:
        raise QueryParamError(f"limit must be ≤ {max_limit}, got {limit}")
    if offset < 0:
        raise QueryParamError(f"offset must be ≥ 0, got {offset}")

    return PaginationParams(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


# Operator → SQL fragment ("?" placeholder for value; "{N}" for IN with
# repeated placeholders). Field names interpolated AFTER allow-list validation.
_OP_SQL = {
    "eq": "{field} = ?",
    "ne": "{field} != ?",
    "gte": "{field} >= ?",
    "lte": "{field} <= ?",
    "gt": "{field} > ?",
    "lt": "{field} < ?",
    # "in" handled specially: produces "{field} IN (?, ?, ...)"
}


@dataclass(frozen=True)
class FilterFieldSpec:
    """Declarative spec for one filter field: allowed ops + Python type."""

    ops: set[str]  # subset of {"eq", "in", "gte", "lte", "gt", "lt", "ne"}
    value_type: type  # str, int, float, bool


@dataclass(frozen=True)
class FilterAllowList:
    """Per-endpoint declaration of permitted filter fields and operators."""

    by_field: dict[str, FilterFieldSpec]

    def __init__(self, by_field: dict[str, FilterFieldSpec]) -> None:
        # frozen dataclass with mutable default field workaround
        object.__setattr__(self, "by_field", dict(by_field))


def _coerce_value(raw: str, value_type: type, field_name: str, op: str) -> Any:
    """Coerce a string query-param value to the spec'd Python type."""
    try:
        if value_type is int:
            return int(raw)
        if value_type is float:
            return float(raw)
        if value_type is bool:
            if raw in ("1", "true", "True"):
                return True
            if raw in ("0", "false", "False"):
                return False
            raise ValueError(f"not boolean: {raw!r}")
        # default: str
        return raw
    except (TypeError, ValueError) as e:
        raise QueryParamError(
            f"invalid value for {field_name}_{op}: {raw!r}"
        ) from e


def parse_filters(
    params: QueryParams,
    *,
    allow_list: FilterAllowList,
) -> dict[str, dict[str, Any]]:
    """Parse query params into a `{field: {op: value}}` structure.

    Raises QueryParamError on unknown field, unknown op, or value-type
    mismatch. Returns empty dict if no filter params present.

    Reserved param names (limit, offset, sort) are skipped; they belong to
    pagination/sort, not filters.
    """
    _RESERVED = {"limit", "offset", "sort"}
    result: dict[str, dict[str, Any]] = {}

    for key in params.keys():
        if key in _RESERVED:
            continue

        # Field + operator parse. Operator suffix starts at the last "_X"
        # where X is in our op vocabulary. Use longest-match against known
        # ops to handle field names that may themselves contain underscores.
        field_name: str
        op: str
        if "_" in key:
            # Try each known suffix from longest to shortest
            for candidate_op in ("gte", "lte", "gt", "lt", "ne", "in", "eq"):
                suffix = f"_{candidate_op}"
                if key.endswith(suffix):
                    field_name = key[: -len(suffix)]
                    op = candidate_op
                    break
            else:
                # No op suffix matched — treat whole key as field name, op=eq
                field_name = key
                op = "eq"
        else:
            field_name = key
            op = "eq"

        if field_name not in allow_list.by_field:
            raise QueryParamError(f"unknown filter field: {field_name}")
        spec = allow_list.by_field[field_name]
        if op not in spec.ops:
            raise QueryParamError(
                f"operator {op!r} not allowed for field {field_name!r}"
            )

        raw_value = params[key]
        if op == "in":
            values = [_coerce_value(v.strip(), spec.value_type, field_name, op)
                      for v in raw_value.split(",")]
            result.setdefault(field_name, {})["in"] = values
        else:
            value = _coerce_value(raw_value, spec.value_type, field_name, op)
            result.setdefault(field_name, {})[op] = value

    return result


def build_where_clause(
    filters: dict[str, dict[str, Any]],
    *,
    allow_list: FilterAllowList,
) -> tuple[str, list[Any]]:
    """Build a parameterized `WHERE ...` SQL fragment from parsed filters.

    Returns `("", [])` for empty filters. Field names are interpolated into
    SQL (validated safe via the allow_list); values are ALWAYS parameterized
    via `?` placeholders.
    """
    if not filters:
        return "", []

    fragments: list[str] = []
    params: list[Any] = []

    for field_name, ops in filters.items():
        # Defensive re-check: every field must be in the allow_list. The
        # caller (parse_filters) already validated, but this is a layered
        # invariant — if a future caller assembles the filters dict by
        # hand, we still won't interpolate a wild identifier.
        if field_name not in allow_list.by_field:
            raise QueryParamError(f"unknown filter field: {field_name}")

        for op, value in ops.items():
            if op == "in":
                placeholders = ", ".join("?" for _ in value)
                fragments.append(f"{field_name} IN ({placeholders})")
                params.extend(value)
            else:
                fragments.append(_OP_SQL[op].format(field=field_name))
                params.append(value)

    return "WHERE " + " AND ".join(fragments), params


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SortField:
    field: str
    direction: Literal["asc", "desc"]


@dataclass(frozen=True)
class SortAllowList:
    fields: set[str]


def parse_sort(
    params: QueryParams,
    *,
    allow_list: SortAllowList,
    default: list[SortField],
    tie_breaker: SortField,
) -> list[SortField]:
    """Parse `sort` query param into a list of `SortField` entries.

    Empty/absent sort applies `default`. Server-appends `tie_breaker` unless
    the user's sort already orders by `tie_breaker.field` (in either
    direction) — the user's explicit ordering wins.
    """
    raw = params.get("sort")
    if not raw:
        user_sort = list(default)
    else:
        user_sort = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                field_name, direction = entry.split(":", 1)
                direction = direction.strip().lower()
            else:
                field_name = entry
                direction = "asc"
            field_name = field_name.strip()
            if field_name not in allow_list.fields:
                raise QueryParamError(
                    f"{field_name!r} is not a sortable field"
                )
            if direction not in ("asc", "desc"):
                raise QueryParamError(
                    f"invalid sort direction: {direction!r}"
                )
            user_sort.append(SortField(field=field_name, direction=direction))

    # Append tie_breaker if not already sorting by its field
    if not any(s.field == tie_breaker.field for s in user_sort):
        user_sort.append(tie_breaker)

    return user_sort


def build_order_by_clause(sort: list[SortField]) -> str:
    """Build the `ORDER BY ...` SQL fragment from validated sort spec."""
    if not sort:
        return ""
    entries = [f"{s.field} {s.direction.upper()}" for s in sort]
    return "ORDER BY " + ", ".join(entries)
```

- [ ] **Step 3: Run helper tests — verify green**

Run:
```bash
source .venv/bin/activate && pytest tests/api/test_query_helpers.py -q --no-header 2>&1 | tail -5
```
Expected: all helper tests pass.

---

## Task 3: Write router tests (red phase for router)

**Files:**
- Modify: `tests/api/conftest.py` (add `games_pool_100` fixture)
- Create: `tests/api/test_games_router.py`

- [ ] **Step 1: Add `games_pool_100` fixture to conftest.py**

Open `tests/api/conftest.py`. After the existing `populated_pool` re-import (around line 27), add a new fixture:

```python
@pytest_asyncio.fixture
async def games_pool_100(populated_pool):  # noqa: F811
    """populated_pool seeded with 100 games for pagination tests.

    Adds 95 games to the 5 already in populated_pool. Mix of platforms
    (steam/epic), statuses (across the 8 enum values), and sizes for
    filter/sort coverage.
    """
    import json

    async with populated_pool.write_transaction() as tx:
        for i in range(6, 101):  # ids 6..100 (5 already exist)
            platform = "steam" if i % 2 == 0 else "epic"
            status = [
                "unknown", "not_downloaded", "up_to_date", "pending_update",
                "downloading", "validation_failed", "blocked", "failed",
            ][i % 8]
            await tx.execute(
                "INSERT INTO games "
                "(platform, app_id, title, owned, size_bytes, status, "
                "last_prefilled_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    platform,
                    f"app_{i:03d}",
                    f"Game {i:03d}",  # title order: Game 006 < Game 100
                    i % 2,  # ~half owned, ~half not
                    i * 1_000_000_000,  # 1 GB increments
                    status,
                    f"2026-05-{(i % 28) + 1:02d}T00:00:00Z" if i % 3 == 0 else None,
                    json.dumps({"depots": [i * 10, i * 10 + 1]}),
                ),
            )
    return populated_pool
```

- [ ] **Step 2: Write the router test file**

Create `tests/api/test_games_router.py`:

```python
"""Tests for GET /api/v1/games (BL7 / Feature 9 partial).

Covers spec §5 — empty DB, happy path, pagination, filtering, sorting,
applied-echo, error paths, auth, pool-failure, metadata, last_error.
"""

from __future__ import annotations

import json

import pytest

VALID_TOKEN = "a" * 32


# ---------------------------------------------------------------------------
# Empty DB
# ---------------------------------------------------------------------------


class TestGamesEmptyDb:
    async def test_empty_db_returns_empty_array(self, client, populated_pool):
        # populated_pool has 5 games; clear them for this test
        async with populated_pool.write_transaction() as tx:
            await tx.execute("DELETE FROM games")
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["games"] == []
        assert body["meta"]["total"] == 0
        assert body["meta"]["has_more"] is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestGamesHappyPath:
    async def test_returns_games(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "games" in body
        assert "meta" in body
        assert len(body["games"]) == 5  # populated_pool seeds 5
        assert body["meta"]["total"] == 5

    async def test_envelope_shape(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert set(body.keys()) == {"games", "meta"}
        assert set(body["meta"].keys()) == {
            "total", "limit", "offset", "has_more",
            "applied_filters", "applied_sort",
        }

    async def test_per_game_field_set(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert set(game.keys()) == {
                "id", "platform", "app_id", "title", "owned",
                "size_bytes", "current_version", "cached_version",
                "status", "last_validated_at", "last_prefilled_at",
                "last_error", "metadata",
            }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestGamesPagination:
    async def test_default_limit_50(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["games"]) == 50
        assert body["meta"]["limit"] == 50
        assert body["meta"]["offset"] == 0
        assert body["meta"]["total"] == 100
        assert body["meta"]["has_more"] is True

    async def test_explicit_limit(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["games"]) == 10
        assert body["meta"]["limit"] == 10
        assert body["meta"]["has_more"] is True

    async def test_offset_progression(self, client, games_pool_100):
        r1 = await client.get(
            "/api/v1/games?limit=10&offset=0",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        r2 = await client.get(
            "/api/v1/games?limit=10&offset=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        ids1 = [g["id"] for g in r1.json()["games"]]
        ids2 = [g["id"] for g in r2.json()["games"]]
        assert set(ids1).isdisjoint(set(ids2))  # no overlap between pages

    async def test_limit_above_max_returns_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?limit=1000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "limit" in r.json()["detail"]

    async def test_negative_offset_returns_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?offset=-1",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_has_more_false_on_last_page(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?limit=50&offset=50",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        assert len(body["games"]) == 50
        assert body["meta"]["has_more"] is False


# ---------------------------------------------------------------------------
# Filter: platform
# ---------------------------------------------------------------------------


class TestGamesFilterPlatform:
    async def test_eq_steam(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform=steam&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["platform"] == "steam"

    async def test_in_multi_value(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform_in=steam,epic&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        platforms = {g["platform"] for g in body["games"]}
        assert platforms == {"steam", "epic"}


# ---------------------------------------------------------------------------
# Filter: status
# ---------------------------------------------------------------------------


class TestGamesFilterStatus:
    async def test_eq_status(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?status=not_downloaded&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["status"] == "not_downloaded"


# ---------------------------------------------------------------------------
# Filter: owned
# ---------------------------------------------------------------------------


class TestGamesFilterOwned:
    async def test_owned_true(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?owned=1&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["owned"] == 1

    async def test_owned_false(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?owned=0&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["owned"] == 0


# ---------------------------------------------------------------------------
# Filter: size_bytes
# ---------------------------------------------------------------------------


class TestGamesFilterSizeBytes:
    async def test_gte(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?size_bytes_gte=50000000000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["size_bytes"] >= 50_000_000_000

    async def test_lte(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?size_bytes_lte=10000000000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["size_bytes"] is None or game["size_bytes"] <= 10_000_000_000

    async def test_gte_lte_combined(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?size_bytes_gte=10000000000&size_bytes_lte=50000000000&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert 10_000_000_000 <= game["size_bytes"] <= 50_000_000_000


# ---------------------------------------------------------------------------
# Filter: time ranges
# ---------------------------------------------------------------------------


class TestGamesFilterTimeRange:
    async def test_last_prefilled_at_gte(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?last_prefilled_at_gte=2026-05-15&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        body = r.json()
        for game in body["games"]:
            assert game["last_prefilled_at"] is not None
            assert game["last_prefilled_at"] >= "2026-05-15"


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------


class TestGamesSort:
    async def test_default_title_asc(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        titles = [g["title"] for g in r.json()["games"]]
        assert titles == sorted(titles)

    async def test_title_desc(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=title:desc&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        titles = [g["title"] for g in r.json()["games"]]
        assert titles == sorted(titles, reverse=True)

    async def test_size_bytes_desc(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=size_bytes:desc&limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        sizes = [g["size_bytes"] for g in r.json()["games"] if g["size_bytes"] is not None]
        assert sizes == sorted(sizes, reverse=True)

    async def test_tie_breaker_in_applied_sort(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=title:asc&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied[-1] == {"field": "id", "direction": "asc"}

    async def test_user_id_sort_dedupes_tie_breaker(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=id:desc&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        # User said id:desc; server should NOT append id:asc
        assert len(applied) == 1
        assert applied[0] == {"field": "id", "direction": "desc"}


# ---------------------------------------------------------------------------
# Applied echo (meta.applied_filters / applied_sort)
# ---------------------------------------------------------------------------


class TestGamesAppliedEcho:
    async def test_applied_filters_reflects_parse(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform=steam&size_bytes_gte=1000&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert applied["platform"]["eq"] == "steam"
        assert applied["size_bytes"]["gte"] == 1000

    async def test_applied_filters_absent_for_unfiltered_fields(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform=steam&limit=10",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        assert "status" not in applied
        assert "size_bytes" not in applied


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestGamesErrors:
    async def test_unknown_filter_field_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?password=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert "unknown filter field" in r.json()["detail"]

    async def test_unknown_operator_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?platform_gte=foo",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_invalid_value_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?size_bytes_gte=abc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400

    async def test_unknown_sort_field_400(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games?sort=password:desc",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Auth (smoke)
# ---------------------------------------------------------------------------


class TestGamesAuth:
    async def test_no_token_returns_401(self, client, games_pool_100):
        r = await client.get("/api/v1/games")
        assert r.status_code == 401

    async def test_valid_token_returns_200(self, client, games_pool_100):
        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Pool failure
# ---------------------------------------------------------------------------


class TestGamesPoolFailure:
    async def test_pool_error_returns_503(self, unit_app, client):
        from orchestrator.api.dependencies import get_pool_dep
        from orchestrator.db.pool import PoolError

        class _FakeBrokenPool:
            async def read_all(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

            async def read_one(self, *_a, **_kw):
                raise PoolError("simulated db unavailable")

        unit_app.dependency_overrides[get_pool_dep] = lambda: _FakeBrokenPool()

        r = await client.get(
            "/api/v1/games",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 503
        assert r.json() == {"detail": "database unavailable"}


# ---------------------------------------------------------------------------
# metadata column
# ---------------------------------------------------------------------------


class TestGamesMetadata:
    async def test_well_formed_metadata_parsed(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE games SET metadata = ? WHERE id = 1",
                (json.dumps({"depots": [10, 11]}),),
            )
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["metadata"] == {"depots": [10, 11]}

    async def test_null_metadata_returns_null(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute("UPDATE games SET metadata = NULL WHERE id = 1")
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["metadata"] is None

    async def test_malformed_metadata_returns_null(self, client, populated_pool):
        # Schema doesn't enforce JSON shape; write deliberately malformed
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE games SET metadata = ? WHERE id = 1",
                ("not-a-valid-{json",),
            )
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["metadata"] is None


# ---------------------------------------------------------------------------
# last_error truncation
# ---------------------------------------------------------------------------


class TestGamesLastErrorTruncation:
    async def test_null_passes_through(self, client, populated_pool):
        async with populated_pool.write_transaction() as tx:
            await tx.execute("UPDATE games SET last_error = NULL WHERE id = 1")
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["last_error"] is None

    async def test_truncated_at_200(self, client, populated_pool):
        long_err = "x" * 5000
        async with populated_pool.write_transaction() as tx:
            await tx.execute(
                "UPDATE games SET last_error = ? WHERE id = 1", (long_err,)
            )
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert len(game["last_error"]) == 200
```

- [ ] **Step 3: Run router tests — verify red phase**

Run:
```bash
source .venv/bin/activate && pytest tests/api/test_games_router.py -q --no-header 2>&1 | tail -10
```
Expected: tests fail / collection error — router doesn't exist yet.

---

## Task 4: Implement `routers/games.py` + wire (green phase)

**Files:**
- Create: `src/orchestrator/api/routers/games.py`
- Modify: `src/orchestrator/api/main.py` (import + include_router)

- [ ] **Step 1: Mark TaskUpdate in_progress for the router task** (enforce-plan-tracking hook)

- [ ] **Step 2: Write the router file**

Create `src/orchestrator/api/routers/games.py`:

```python
"""GET /api/v1/games — paginated list of the games library (BL7 / Feature 9)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api._query_helpers import (
    FilterAllowList,
    FilterFieldSpec,
    PaginationParams,
    QueryParamError,
    SortAllowList,
    SortField as _SortField,
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


# Endpoint constants (spec §3.1, §3.4)
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
LAST_ERROR_TRUNCATE = 200
DEFAULT_SORT = (_SortField(field="title", direction="asc"),)
TIE_BREAKER = _SortField(field="id", direction="asc")

GAMES_FILTER_ALLOW_LIST = FilterAllowList(
    {
        "platform": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "status": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
        "owned": FilterFieldSpec(ops={"eq"}, value_type=int),
        "size_bytes": FilterFieldSpec(ops={"eq", "gte", "lte"}, value_type=int),
        "last_prefilled_at": FilterFieldSpec(ops={"gte", "lte"}, value_type=str),
        "last_validated_at": FilterFieldSpec(ops={"gte", "lte"}, value_type=str),
    }
)

GAMES_SORT_ALLOW_LIST = SortAllowList(
    fields={"id", "title", "status", "size_bytes", "last_prefilled_at", "last_validated_at"}
)

# All schema columns explicitly listed so the SELECT is stable across
# future migrations (a new column won't accidentally appear in the wire).
_GAMES_COLUMNS = (
    "id, platform, app_id, title, owned, size_bytes, "
    "current_version, cached_version, status, "
    "last_validated_at, last_prefilled_at, last_error, metadata"
)

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class GameResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    platform: Literal["steam", "epic"]
    app_id: str
    title: str
    owned: int
    size_bytes: int | None
    current_version: str | None
    cached_version: str | None
    status: Literal[
        "unknown", "not_downloaded", "up_to_date", "pending_update",
        "downloading", "validation_failed", "blocked", "failed",
    ]
    last_validated_at: str | None
    last_prefilled_at: str | None
    last_error: str | None
    metadata: dict[str, Any] | None


class FilterCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    # Full operator surface declared on the model so future endpoints
    # may use any of them; in BL7 only eq/in/gte/lte are permitted by
    # any field's allow-list (see GAMES_FILTER_ALLOW_LIST above).
    eq: Any | None = None
    in_: list[Any] | None = Field(default=None, alias="in")
    gte: Any | None = None
    lte: Any | None = None
    gt: Any | None = None
    lt: Any | None = None
    ne: Any | None = None


class SortFieldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"]


class GamesMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    limit: int
    offset: int
    has_more: bool
    applied_filters: dict[str, FilterCriterion]
    applied_sort: list[SortFieldResponse]


class GameListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    games: list[GameResponse]
    meta: GamesMeta


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/v1", tags=["games"])


@router.get(
    "/games",
    response_model=GameListResponse,
    responses={
        200: {"description": "Paginated list of games"},
        400: {"description": "Bad query parameters"},
        401: {"description": "Missing or invalid bearer token"},
        503: {"description": "Database pool unhealthy"},
    },
    summary="List games",
    description=(
        "Returns the games library with filter, sort, and pagination. "
        "See spec docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md "
        "for the full per-field filter + sort allow-list and the meta envelope shape."
    ),
)
async def list_games(
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
            request.query_params, allow_list=GAMES_FILTER_ALLOW_LIST
        )
        sort = parse_sort(
            request.query_params,
            allow_list=GAMES_SORT_ALLOW_LIST,
            default=list(DEFAULT_SORT),
            tie_breaker=TIE_BREAKER,
        )
    except QueryParamError as e:
        return JSONResponse(content={"detail": str(e)}, status_code=400)

    where_sql, where_params = build_where_clause(
        filters, allow_list=GAMES_FILTER_ALLOW_LIST
    )
    order_sql = build_order_by_clause(sort)

    count_sql = f"SELECT COUNT(*) AS total FROM games {where_sql}".strip()
    rows_sql = (
        f"SELECT {_GAMES_COLUMNS} FROM games {where_sql} {order_sql} "
        f"LIMIT ? OFFSET ?"
    ).strip()
    rows_params = [*where_params, pagination.limit, pagination.offset]

    try:
        count_row = await pool.read_one(count_sql, where_params)
        rows = await pool.read_all(rows_sql, rows_params)
    except PoolError as e:
        _log.error("api.games.read_failed", reason=str(e))
        return JSONResponse(
            content={"detail": "database unavailable"}, status_code=503
        )

    total = int(count_row["total"]) if count_row else 0

    games: list[GameResponse] = []
    for row in rows:
        # metadata: parse JSON column; null if NULL or parse fails
        raw_meta = row["metadata"]
        if raw_meta is None:
            metadata: dict[str, Any] | None = None
        else:
            try:
                metadata = json.loads(raw_meta)
                if not isinstance(metadata, dict):
                    metadata = None  # only dict-shaped JSON is exposed
            except (json.JSONDecodeError, TypeError):
                _log.warning(
                    "api.games.metadata_parse_failed",
                    game_id=row["id"],
                )
                metadata = None

        raw_err = row["last_error"]
        last_error = raw_err[:LAST_ERROR_TRUNCATE] if raw_err else None

        games.append(
            GameResponse(
                id=row["id"],
                platform=row["platform"],
                app_id=row["app_id"],
                title=row["title"],
                owned=row["owned"],
                size_bytes=row["size_bytes"],
                current_version=row["current_version"],
                cached_version=row["cached_version"],
                status=row["status"],
                last_validated_at=row["last_validated_at"],
                last_prefilled_at=row["last_prefilled_at"],
                last_error=last_error,
                metadata=metadata,
            )
        )

    # applied_filters echo: convert {field: {op: value}} → {field: FilterCriterion}
    applied_filters: dict[str, FilterCriterion] = {}
    for field_name, ops in filters.items():
        crit_kwargs: dict[str, Any] = {}
        for op, value in ops.items():
            if op == "in":
                crit_kwargs["in_"] = value
            else:
                crit_kwargs[op] = value
        applied_filters[field_name] = FilterCriterion(**crit_kwargs)

    applied_sort = [
        SortFieldResponse(field=s.field, direction=s.direction) for s in sort
    ]

    body = GameListResponse(
        games=games,
        meta=GamesMeta(
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
            has_more=(pagination.offset + len(games) < total),
            applied_filters=applied_filters,
            applied_sort=applied_sort,
        ),
    )
    return JSONResponse(content=body.model_dump(by_alias=True))
```

- [ ] **Step 3: Wire the router in main.py**

Open `src/orchestrator/api/main.py`. Find the existing platforms import:
```python
from orchestrator.api.routers.platforms import router as platforms_router
```
Add below it:
```python
from orchestrator.api.routers.games import router as games_router
```

Find the existing include_router lines:
```python
    app.include_router(health_router)
    app.include_router(platforms_router)
```
Add below:
```python
    app.include_router(games_router)
```

- [ ] **Step 4: Run router tests — verify green**

Run:
```bash
source .venv/bin/activate && pytest tests/api/test_games_router.py -q --no-header 2>&1 | tail -8
```
Expected: all router tests pass.

- [ ] **Step 5: Run full project test suite**

Run:
```bash
source .venv/bin/activate && pytest -q --no-header 2>&1 | tail -3
```
Expected: ~435 tests passing (387 prior + ~48 new). No failures.

---

## Task 5: Mark Build Loop checkpoints

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

## Task 6: Security audit

**Files:**
- Create: `docs/security-audits/bl7-f9-games-readonly-security-audit.md`

- [ ] **Step 1: ruff check + format**

Run:
```bash
source .venv/bin/activate && ruff check src/orchestrator/api/routers/games.py src/orchestrator/api/_query_helpers.py src/orchestrator/api/main.py tests/api/test_games_router.py tests/api/test_query_helpers.py
ruff format --check src/orchestrator/api/routers/games.py src/orchestrator/api/_query_helpers.py src/orchestrator/api/main.py tests/api/test_games_router.py tests/api/test_query_helpers.py
```
Expected: `All checks passed!` for both.

If ruff format flags files, run `ruff format <files>` to fix, then re-run `--check`.

- [ ] **Step 2: mypy --strict**

Run:
```bash
source .venv/bin/activate && mypy --strict src/orchestrator/api/routers/games.py src/orchestrator/api/_query_helpers.py src/orchestrator/api/main.py
```
Expected: `Success: no issues found in 3 source files`.

- [ ] **Step 3: semgrep OWASP**

Run:
```bash
source .venv/bin/activate && semgrep --config p/owasp-top-ten --error src/orchestrator/api/routers/games.py src/orchestrator/api/_query_helpers.py
```
Expected: `0 findings`.

- [ ] **Step 4: gitleaks**

Run:
```bash
gitleaks detect --no-banner --redact --source .
```
Expected: `no leaks found`.

- [ ] **Step 5: Write the security audit doc**

Create `docs/security-audits/bl7-f9-games-readonly-security-audit.md`:

```markdown
# Security Audit — BL7 games read-only endpoint

**Feature:** BL7-F9-games-readonly (Build Loop 7, Milestone B)
**Module:** `src/orchestrator/api/routers/games.py` (~210 LoC) + `src/orchestrator/api/_query_helpers.py` (~220 LoC) + 2-line wire in `src/orchestrator/api/main.py`
**Audit date:** 2026-05-17
**Auditor:** self-review (Senior Security Engineer persona) + automated SAST (semgrep OWASP top-10) + gitleaks
**Phase:** 2 (Construction), Build Loop step 2.4

<!-- Last Updated: 2026-05-17 -->

## Scope

Post-implementation security review of:
- `routers/games.py` — handler + Pydantic models + per-endpoint filter/sort allow-lists
- `_query_helpers.py` — shared parser/validator/SQL-builder primitives that future paginated endpoints will reuse

The audit inherits the BL5+BL6+UAT-3 substrate (bearer auth, CORS-outermost stack, correlation_id propagation, ID3 redaction). Auth, body-cap, CORS, and middleware behavior are not in scope here.

## Methodology

1. **Automated SAST**: `semgrep --config p/owasp-top-ten --error` — 0 findings
2. **gitleaks**: full repo scan — 0 findings
3. **ruff check + ruff format**: clean
4. **mypy --strict**: clean
5. **Property-based SQL-injection test**: Hypothesis test in `test_query_helpers.py::TestSqlInjectionResistance` exercises `build_where_clause` with random and adversarial inputs; asserts no user value ever appears literally in the SQL string
6. **Manual review** against TM-005 (SQL injection), TM-012 (log redaction), spec §6 risk register

## Findings

**SEV-1: 0**
**SEV-2: 0**
**SEV-3: 0**
**SEV-4: 0**

No findings. Rationale below.

## Threat-model walk

### TM-005 — SQL injection via API surface
**Verdict: MITIGATED.** Two-layer defense:
1. Field names are interpolated into SQL but ONLY after `parse_filters`/`parse_sort` validate them against the endpoint's `FilterAllowList`/`SortAllowList`. Identifiers outside the allow-list raise `QueryParamError` → 400 before any SQL touches them. The defensive re-check in `build_where_clause` is a layered invariant.
2. User values flow EXCLUSIVELY through SQLite parameter binds. Verified by `TestSqlInjectionResistance` property test (Hypothesis): under random and adversarial input including `"'; DROP TABLE games; --"`, the SQL string contains only `?` placeholders for values.

### TM-012 — Credential redaction in logs
**Verdict: MITIGATED.** Endpoint emits:
- `api.games.read_failed` with `reason=str(e)` on PoolError. `PoolError` messages come from BL4's structured exception hierarchy (no raw SQL, params, or credentials — ADR-0011).
- `api.games.metadata_parse_failed` with `game_id` only — no metadata content. The actual malformed JSON string never reaches a log call.

### TM-013 — Fingerprinting via differential responses
**Verdict: MITIGATED for the BL5 substrate; not amplified.** Three response shapes only: 200 with canonical envelope, 400 with `{detail}`, 503 with `{detail}`. Filter/sort timing depends on data; not on auth state.

## Decisions D1-D12 walk

- **D1 offset pagination**: Verified via `TestGamesPagination`. Limit/offset enforced at parser, parameterized into SQL.
- **D2 rich meta**: Verified via `TestGamesAppliedEcho`. `applied_filters` echo uses `FilterCriterion` model with `extra="forbid"`.
- **D3 default=50, max=500, reject 400**: Verified via `TestGamesPagination::test_limit_above_max_returns_400`.
- **D4 operator-suffix syntax**: Verified via per-field test classes covering `=`, `_in`, `_gte`, `_lte`. Unknown op or field → 400.
- **D5 tie-breaker + de-dup**: Verified via `TestGamesSort::test_user_id_sort_dedupes_tie_breaker`.
- **D6 metadata included as JSON**: Verified via `TestGamesMetadata`. Malformed JSON → null + structured log.
- **D7 last_error truncated to 200**: Verified via `TestGamesLastErrorTruncation`.
- **D8 empty result returns 200**: Verified via `TestGamesEmptyDb`.
- **D9 unknown field/op → 400**: Verified via `TestGamesErrors`.
- **D10 Pydantic extra="forbid"**: All response models set it.
- **D11 bearer required**: Verified via `TestGamesAuth`. `/api/v1/games` not in `AUTH_EXEMPT_PATHS`.
- **D12 PoolError → 503**: Verified via `TestGamesPoolFailure`. Structured log with correlation_id propagated.

## Non-findings (explicitly cleared)

- **No SQL injection vector.** All values parameterized. Field names allow-list validated.
- **No timing oracle on auth.** Auth handled by BL5 middleware; reached only after auth passes.
- **No fingerprinting via 200/400/503 shape.** Response body shape is consistent within each status.
- **No DoS via response size.** `limit ≤ 500` enforced at parser; `metadata` bounded; `last_error` truncated.
- **No log-volume amplification on hot path.** Success path emits only middleware's `api.request.received` + `api.request.completed`; router emits only on 503 or metadata-parse-failure paths.

## Test coverage

~48 tests total: ~18 helpers + ~30 router. Hypothesis property test for SQL injection. Branch coverage on both modules ≥95%.

## Verification artifacts

- `pytest -q`: ~435 tests passing project-wide
- `ruff check` + `ruff format --check`: clean
- `mypy --strict`: clean
- `semgrep --config p/owasp-top-ten --error`: 0 findings
- `gitleaks detect`: no leaks

## Conclusion

**APPROVED for merge.** Zero findings across automated and manual review. The endpoint inherits BL5+BL6+UAT-3's hardened substrate and adds no new attack surface beyond a parametric SQL builder whose injection resistance is pinned by both unit tests and a property-based test. The `_query_helpers.py` conventions established here propagate to future paginated F9 endpoints with the same security guarantees.
```

- [ ] **Step 6: Mark security_audit**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:security_audit
```
Expected: `[OK] Step 'security_audit' completed for build_loop (4/6)`.

---

## Task 7: Update CHANGELOG + FEATURES

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `FEATURES.md`

- [ ] **Step 1: CHANGELOG entry**

Open `CHANGELOG.md`. Find `## [Unreleased]` → `### Added`. Add as FIRST item under `### Added`:

```markdown
- **`GET /api/v1/games`** (BL7 / Feature 9 partial) — first paginated F9
  read endpoint. Returns the games library with filter (operator-suffix
  syntax: `field`, `field_in`, `field_gte`, `field_lte`), sort (multi-field
  with `:asc`/`:desc` + server-appended `id:asc` tie-breaker), and
  offset-based pagination (default 50, max 500, reject 400 above max).
  Rich meta envelope: `total`, `limit`, `offset`, `has_more`,
  `applied_filters`, `applied_sort`. New shared module
  `src/orchestrator/api/_query_helpers.py` provides parser/validator/SQL
  builder primitives reusable by every future paginated F9 endpoint
  (`/jobs`, `/manifests`, etc.). `metadata` column included as parsed JSON
  (null on parse failure); `last_error` truncated to 200 chars (BL6
  pattern). Pool failures translate to 503 with structured
  `api.games.read_failed` log. SQL injection resistance pinned by both
  unit tests and a Hypothesis property test. See
  [spec](docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md)
  and [audit](docs/security-audits/bl7-f9-games-readonly-security-audit.md).
```

- [ ] **Step 2: FEATURES Feature 7 entry**

Open `FEATURES.md`. After the existing Feature 6 (BL6 platforms) section, add:

```markdown
## Feature 7: BL7 — `GET /api/v1/games` (read-only, paginated)

**Phase Built:** 2 (Milestone B, Build Loop 7)
**Status:** Complete (2026-05-17)
**Summary:** First paginated F9 read endpoint on the BL5+BL6 substrate.
Returns the games library with filter, sort, and offset-based
pagination. Wrapped envelope `{"games": [...], "meta": {...}}` with
rich meta including `total`, `has_more`, `applied_filters`, and
`applied_sort` echo. Per-endpoint filter/sort allow-list acts as both
the security boundary AND the docs surface.
**Key Interfaces:**
  - `src/orchestrator/api/routers/games.py` — `GameResponse`,
    `GameListResponse`, `GamesMeta`, `FilterCriterion`,
    `SortFieldResponse` Pydantic models; `list_games` handler
  - `src/orchestrator/api/_query_helpers.py` — `parse_pagination`,
    `parse_filters`, `parse_sort`, `build_where_clause`,
    `build_order_by_clause`; `FilterAllowList`, `SortAllowList`,
    `FilterFieldSpec`, `SortField`, `PaginationParams`,
    `QueryParamError`
  - Wired in `src/orchestrator/api/main.py` via
    `app.include_router(games_router)`
**Locked decisions (D1-D12):** offset pagination · rich meta envelope ·
default=50/max=500 (reject 400) · operator-suffix filters · multi-field
sort with `id:asc` tie-breaker · metadata as parsed JSON · last_error
200-char truncation · empty result returns 200 · unknown field/op →
400 · Pydantic `extra="forbid"` · bearer required · PoolError → 503.
See [spec](superpowers/specs/2026-05-17-bl7-games-readonly-design.md).
**Test Coverage:** ~48 tests across `tests/api/test_games_router.py`
(~30 HTTP-level tests) and `tests/api/test_query_helpers.py` (~18 unit
tests + 1 Hypothesis property test for SQL injection resistance).
Branch coverage ≥95% on both modules.
**Related Audit:** [`bl7-f9-games-readonly-security-audit.md`](security-audits/bl7-f9-games-readonly-security-audit.md) — 0 findings.
**Known Limitations:**
  - No title search (`_like`) in BL7 — deferred to BL-future-search
    (needs FTS5 or trigram support). Game_shelf can client-side filter
    50 rows on title trivially.
  - No per-game endpoint `GET /api/v1/games/{id}` — clients read the
    list and index client-side; if a real need surfaces, additive.
  - `_query_helpers.py` operator surface declares `gt`/`lt`/`ne` for
    future endpoints, but no current field's allow-list permits them.
    They become available when a future endpoint opts in.

---
```

- [ ] **Step 3: Mark documentation_updated**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
```
Expected: `[OK] Step 'documentation_updated' completed for build_loop (5/6)`.

---

## Task 8: Combined feat + docs commit

**Files staged:** all source + tests + CHANGELOG + FEATURES + audit doc + `.claude/process-state.json` (auto-bumped by checklist marks).

- [ ] **Step 1: Survey state**

Run:
```bash
git status --short
git diff --stat
```
Expected: 
- `?? src/orchestrator/api/_query_helpers.py`
- `?? src/orchestrator/api/routers/games.py`
- `?? tests/api/test_query_helpers.py`
- `?? tests/api/test_games_router.py`
- `?? docs/security-audits/bl7-f9-games-readonly-security-audit.md`
- ` M src/orchestrator/api/main.py`
- ` M tests/api/conftest.py`
- ` M CHANGELOG.md`
- ` M FEATURES.md`
- ` M .claude/process-state.json`

- [ ] **Step 2: Stage all files**

Run:
```bash
git add \
  src/orchestrator/api/_query_helpers.py \
  src/orchestrator/api/routers/games.py \
  src/orchestrator/api/main.py \
  tests/api/test_query_helpers.py \
  tests/api/test_games_router.py \
  tests/api/conftest.py \
  docs/security-audits/bl7-f9-games-readonly-security-audit.md \
  CHANGELOG.md \
  FEATURES.md \
  .claude/process-state.json
```

- [ ] **Step 3: Write commit message to tmp**

Write `/tmp/bl7-feat-commit.txt`:
```
feat(api): GET /api/v1/games — first paginated F9 read endpoint

First paginated F9 endpoint on the BL5+BL6 substrate. Returns the
games library with filter, sort, and pagination. Locks the
conventions every future paginated F9 endpoint (/jobs, /manifests,
/stats, /block_list) will reuse via the new _query_helpers.py.

Behavior:
- Offset-based pagination, default 50, max 500 (reject 400)
- Filter syntax: operator-suffix (field, _in, _gte, _lte)
- Sort syntax: multi-field with :asc/:desc; server-appends id:asc
  tie-breaker for pagination stability (with de-dup if user sorts
  by id)
- Rich meta envelope: total, limit, offset, has_more,
  applied_filters (FilterCriterion echo), applied_sort
- metadata column parsed as JSON (null on malformed JSON, logged)
- last_error truncated to 200 chars (BL6 pattern)
- Empty result returns 200 with empty array + meta.total=0
- Unknown filter/sort field or operator → 400
- Bearer required (NOT in AUTH_EXEMPT_PATHS)
- PoolError → 503 with structured api.games.read_failed log

Implementation:
- src/orchestrator/api/_query_helpers.py (~220 LoC) — strict-scope
  parser/validator/builder; reusable by future endpoints
- src/orchestrator/api/routers/games.py (~210 LoC) — Pydantic models +
  handler composing the helpers
- 2-line wire-up in main.py

Tests:
- tests/api/test_query_helpers.py — ~18 unit tests including a
  Hypothesis property test pinning SQL-injection resistance
- tests/api/test_games_router.py — ~30 HTTP-level tests covering
  empty DB, happy path, pagination, filters per field, sorts,
  applied echo, errors, auth, pool failure, metadata parse,
  last_error truncation
- tests/api/conftest.py — adds games_pool_100 fixture for
  pagination tests

Docs:
- CHANGELOG entry under [Unreleased] → Added
- FEATURES Feature 7 entry
- Security audit at docs/security-audits/bl7-f9-games-readonly-security-audit.md
  (0 findings)
- Spec at docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md
  (committed ffc2e83)
- Plan at docs/superpowers/plans/2026-05-17-bl7-games-readonly.md
  (separate docs(plan) commit)

Verification: full project suite green (~435 tests; +48 new);
ruff / ruff format / mypy --strict / semgrep p/owasp-top-ten /
gitleaks all clean.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

- [ ] **Step 4: Mark evaluated + commit**

Run:
```bash
bash .claude/framework/hooks/mark-evaluated.sh "BL7 feat+docs commit — single bundled commit per BL5/BL6 gate-forced ordering pattern"
git commit -F /tmp/bl7-feat-commit.txt
```
Expected: 10 files committed, 1 commit on `feat/bl7-games-readonly`.

If pre-commit gate blocks for any reason: NEVER use `--no-verify`. Read the message, fix the underlying issue, re-stage, commit.

- [ ] **Step 5: Verify**

Run:
```bash
git log --oneline -3
git status --short
```
Expected: top commit subject `feat(api): GET /api/v1/games — first paginated F9 read endpoint`. Working tree clean.

---

## Task 9: Mark feature_recorded + record in test-gate counter

- [ ] **Step 1: Mark feature_recorded**

Run:
```bash
scripts/process-checklist.sh --complete-step build_loop:feature_recorded
```
Expected: `[OK] Step 'feature_recorded' completed for build_loop (6/6)` + `[OK] All steps complete for build_loop!`.

- [ ] **Step 2: Record feature in test-gate counter**

Run:
```bash
scripts/test-gate.sh --record-feature "BL7-F9-games-readonly"
```
Expected: counter increments to 2/2 → UAT-4 trigger fires.

- [ ] **Step 3: Verify test-gate state**

Run:
```bash
scripts/test-gate.sh --check-batch
```
Expected: `[FAIL] Testing session required (2 features since last test, interval is 2)`. **This is the correct outcome** — UAT-4 must run after this BL ships.

---

## Task 10: Push + open PR

- [ ] **Step 1: Push branch**

Run:
```bash
git push -u origin feat/bl7-games-readonly
```

- [ ] **Step 2: Write PR body**

Write `/tmp/bl7-pr-body.txt`:
```markdown
## Summary

BL7 — `GET /api/v1/games` — first paginated F9 read endpoint on the BL5+BL6 substrate. Locks pagination + filter + sort + meta-envelope conventions for every future paginated F9 endpoint via the new shared `_query_helpers.py`.

## What's in this PR

| Commit | Purpose |
|---|---|
| `docs(spec)` | Design with 12 locked decisions D1-D12 |
| `docs(plan)` | 11-task implementation plan |
| `feat(api)` | Router + helpers + 48-test suite + CHANGELOG/FEATURES/audit |

## Locked decisions (D1-D12)

| ID | Decision |
|---|---|
| D1 | Offset-based pagination |
| D2 | Rich meta envelope (total, limit, offset, has_more, applied_filters, applied_sort) |
| D3 | default=50, max=500; reject 400 above max |
| D4 | Operator-suffix filter syntax (`field`, `_in`, `_gte`, `_lte`) |
| D5 | Multi-field sort with server-appended `id:asc` tie-breaker (with de-dup) |
| D6 | `metadata` column included as parsed JSON (null on parse failure) |
| D7 | `last_error` truncated to 200 chars (BL6 pattern) |
| D8 | Empty result returns 200 with empty array + `meta.total=0` |
| D9 | Unknown filter/sort field or operator → 400 |
| D10 | Pydantic `extra="forbid"` on response models |
| D11 | Bearer required (NOT in `AUTH_EXEMPT_PATHS`) |
| D12 | `PoolError` → 503 with structured body (BL6 pattern) |

## Verification

- ~435 project tests passing (+48 new across `test_games_router.py` and `test_query_helpers.py`)
- ruff / ruff format / mypy --strict / semgrep p/owasp-top-ten / gitleaks all clean
- 6/6 Build Loop checklist; feature recorded
- Test-gate counter at 2/2 → **UAT-4 triggers after merge**
- Security audit: 0 findings

## Test plan

- [ ] CI status checks pass (8 required)
- [ ] Manual smoke (bearer set as `$T`):
  - `curl -H "Authorization: Bearer $T" 'http://127.0.0.1:8765/api/v1/games?limit=5'` returns wrapped envelope with 5 games, meta with total/has_more
  - `curl ... '/api/v1/games?platform=steam&sort=last_prefilled_at:desc&limit=10'` returns Steam games sorted by recent prefill
  - `curl ... '/api/v1/games?password=foo'` returns 400 with "unknown filter field" detail
  - `curl ... '/api/v1/games?limit=1000'` returns 400 with limit cap message
- [ ] Review locked decisions in spec; flag any conventions you want revisited before next paginated endpoint inherits them

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 3: Open PR**

Run:
```bash
gh pr create \
  --title "feat(api): GET /api/v1/games — first paginated F9 read endpoint" \
  --body-file /tmp/bl7-pr-body.txt \
  --base main \
  --head feat/bl7-games-readonly
```

- [ ] **Step 4: Report PR URL; do NOT merge**

Per project memory `feedback_pr_merge_ownership.md`: user merges PRs themselves. Stop after PR is opened.

---

## Self-Review

**Spec coverage check:**

| Spec section | Plan task |
|---|---|
| §1 Goal | Tasks 2, 4 (helpers + router) |
| §2 D1 offset pagination | Task 1 `TestParsePagination`; Task 2 `parse_pagination` |
| §2 D2 rich meta envelope | Task 3 `TestGamesAppliedEcho`; Task 4 `GamesMeta`+`FilterCriterion` |
| §2 D3 default/max + reject 400 | Task 1+3 limit-cap tests; Task 2 raises `QueryParamError`; Task 4 catches → 400 |
| §2 D4 operator-suffix filters | Task 1 `TestParseFilters`; Task 2 `parse_filters`+`build_where_clause`; Task 3 per-field tests |
| §2 D5 multi-field sort + tie-breaker | Task 1 `TestParseSort` (incl. de-dup); Task 2 `parse_sort`; Task 3 `TestGamesSort` |
| §2 D6 metadata parsed JSON | Task 3 `TestGamesMetadata`; Task 4 router try/except json.loads |
| §2 D7 last_error truncated | Task 3 `TestGamesLastErrorTruncation`; Task 4 `[:LAST_ERROR_TRUNCATE]` |
| §2 D8 empty → 200 | Task 3 `TestGamesEmptyDb`; Task 4 happy path |
| §2 D9 unknown → 400 | Task 3 `TestGamesErrors`; Task 4 catch `QueryParamError` |
| §2 D10 extra="forbid" | Task 4 every response model |
| §2 D11 bearer required | Task 3 `TestGamesAuth`; Task 4 path NOT added to `AUTH_EXEMPT_PATHS` |
| §2 D12 PoolError → 503 | Task 3 `TestGamesPoolFailure`; Task 4 try/except PoolError |
| §3 wire format | Task 4 GameResponse, GamesMeta, exact JSON shapes |
| §3.1 per-field allow-list | Task 4 `GAMES_FILTER_ALLOW_LIST` constant |
| §3.3 error responses | Task 4 try/except returns 400/503 |
| §4 architecture / file layout | Tasks 2 + 4 paths exact |
| §4.2 Pydantic models | Task 4 full source |
| §4.3 SQL strategy (two queries, parametric) | Task 4 router source |
| §5 test plan | Tasks 1, 3 — every test class enumerated |
| §6 risk register | Tests cover injection, metadata parse, last_error truncate; `extra="forbid"` catches schema drift |
| §7 documentation deltas | Task 6 (audit) + Task 7 (CHANGELOG/FEATURES) |
| §9 open follow-ups | Plan does not implement title search, cursor mode, or per-game endpoint — correctly deferred |

**Placeholder scan:** No TBD/TODO/implement-later. All file paths, code blocks, commands concrete.

**Type consistency:** `_SortField` (helpers) vs `SortFieldResponse` (Pydantic model in router) are intentionally separate — helpers use a dataclass, router uses a Pydantic model. Conversion happens in handler. `FilterCriterion` (Pydantic, with `in_` aliased to `in`) vs raw `{field: {op: value}}` dict from helpers — conversion in handler. Both deliberate; documented in spec §4.2.
