"""Unit tests for orchestrator.api._query_helpers (BL7 / Feature 9 partial).

Covers parser/validator/SQL-builder primitives in isolation. The router
integration tests live in test_games_router.py.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
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
            parse_pagination(QueryParams("offset=-1"), default_limit=50, max_limit=500)

    def test_negative_limit_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_pagination

        with pytest.raises(QueryParamError, match="limit"):
            parse_pagination(QueryParams("limit=-5"), default_limit=50, max_limit=500)

    def test_zero_limit_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_pagination

        with pytest.raises(QueryParamError, match="limit"):
            parse_pagination(QueryParams("limit=0"), default_limit=50, max_limit=500)

    def test_non_numeric_limit_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_pagination

        with pytest.raises(QueryParamError, match="limit"):
            parse_pagination(QueryParams("limit=abc"), default_limit=50, max_limit=500)


# ---------------------------------------------------------------------------
# parse_filters
# ---------------------------------------------------------------------------


def _games_allow_list():
    """Filter allow-list as it will appear in routers/games.py."""
    from orchestrator.api._query_helpers import FilterAllowList, FilterFieldSpec

    return FilterAllowList(
        {
            "platform": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
            "status": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
            "owned": FilterFieldSpec(ops={"eq"}, value_type=int),
            "size_bytes": FilterFieldSpec(ops={"eq", "gte", "lte"}, value_type=int),
            "last_prefilled_at": FilterFieldSpec(ops={"gte", "lte"}, value_type=str),
            "last_validated_at": FilterFieldSpec(ops={"gte", "lte"}, value_type=str),
        }
    )


class TestParseFilters:
    def test_empty_query_returns_empty(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(QueryParams(""), allow_list=_games_allow_list())
        assert result == {}

    def test_single_eq_filter(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(QueryParams("platform=steam"), allow_list=_games_allow_list())
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
        assert result == {"status": {"in": ["not_downloaded", "pending_update"]}}

    def test_in_operator_strips_whitespace_per_value(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("status_in=not_downloaded , pending_update"),
            allow_list=_games_allow_list(),
        )
        assert result == {"status": {"in": ["not_downloaded", "pending_update"]}}

    def test_range_combo(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("size_bytes_gte=1000&size_bytes_lte=5000"),
            allow_list=_games_allow_list(),
        )
        assert result == {"size_bytes": {"gte": 1000, "lte": 5000}}

    def test_int_value_type_coerced(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(QueryParams("owned=1"), allow_list=_games_allow_list())
        assert result == {"owned": {"eq": 1}}
        assert isinstance(result["owned"]["eq"], int)

    def test_unknown_field_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        with pytest.raises(QueryParamError, match="unknown filter field"):
            parse_filters(QueryParams("foo=bar"), allow_list=_games_allow_list())

    def test_unknown_operator_raises(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        with pytest.raises(QueryParamError, match=r"unknown operator|not allowed"):
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

    def test_range_combines_with_and(self):
        from orchestrator.api._query_helpers import build_where_clause

        sql, params = build_where_clause(
            {"size_bytes": {"gte": 100, "lte": 500}},
            allow_list=_games_allow_list(),
        )
        assert sql == "WHERE size_bytes >= ? AND size_bytes <= ?"
        assert params == [100, 500]

    def test_multi_field_ands(self):
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

        sql, _params = build_where_clause(
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
