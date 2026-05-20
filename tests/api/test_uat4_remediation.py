"""Regression tests for UAT-4 remediation (2026-05-20).

Each test class corresponds to one finding from
tests/uat/sessions/2026-05-20-session-4/agent-results/_consolidated.md.
"""

from __future__ import annotations

import pytest
from starlette.datastructures import QueryParams

VALID_TOKEN = "a" * 32


# ---------------------------------------------------------------------------
# S2-A: applied_filters echo wire format — compact {op: value} only
# ---------------------------------------------------------------------------


class TestS2AAppliedFiltersCompactShape:
    async def test_eq_only_emits_single_op_key(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games?platform=steam",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        # Spec §3.2: compact shape — only the applied op key per field
        assert applied == {"platform": {"eq": "steam"}}

    async def test_range_emits_both_op_keys(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games?size_bytes_gte=1000&size_bytes_lte=5000",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        # Two ops on same field: both present, NO null values
        assert applied == {"size_bytes": {"gte": 1000, "lte": 5000}}
        # Explicit no-null assertion
        for ops in applied.values():
            assert None not in ops.values()

    async def test_in_serializes_under_alias_in(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games?status_in=not_downloaded,pending_update",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_filters"]
        # The `in` op (Python attribute would be `in_`) must serialize as `in`
        assert "status" in applied
        assert "in" in applied["status"]
        assert applied["status"]["in"] == ["not_downloaded", "pending_update"]
        # Must NOT leak the Python attribute name
        assert "in_" not in applied["status"]


# ---------------------------------------------------------------------------
# S2-B: ?sort=,,, (empty entries) must apply default + tie-breaker
# ---------------------------------------------------------------------------


class TestS2BEmptySortDoesNotDropDefault:
    async def test_all_empty_entries_apply_default(self):
        from orchestrator.api._query_helpers import SortField, parse_sort

        # When all sort entries are empty after split+strip, default must apply
        result = parse_sort(
            QueryParams("sort=,,,"),
            allow_list=_games_sort_allow_list(),
            default=[SortField(field="title", direction="asc")],
            tie_breaker=SortField(field="id", direction="asc"),
        )
        assert result == [
            SortField(field="title", direction="asc"),
            SortField(field="id", direction="asc"),
        ]

    async def test_via_http(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games?sort=,,,&limit=3",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        applied = r.json()["meta"]["applied_sort"]
        assert applied == [
            {"field": "title", "direction": "asc"},
            {"field": "id", "direction": "asc"},
        ]

    async def test_single_empty_entry_skipped_among_valid(self):
        from orchestrator.api._query_helpers import SortField, parse_sort

        # Some empty + some valid: empties dropped, valids preserved, tie-breaker added
        result = parse_sort(
            QueryParams("sort=,title:desc,,"),
            allow_list=_games_sort_allow_list(),
            default=[SortField(field="title", direction="asc")],
            tie_breaker=SortField(field="id", direction="asc"),
        )
        assert result == [
            SortField(field="title", direction="desc"),
            SortField(field="id", direction="asc"),
        ]


# ---------------------------------------------------------------------------
# S2-C: _in cardinality cap
# ---------------------------------------------------------------------------


class TestS2CInCardinalityCap:
    def test_in_with_100_values_accepted(self):
        from orchestrator.api._query_helpers import parse_filters

        values = ",".join(f"v{i}" for i in range(100))
        # Use 'status' field (str type); the values won't be valid statuses
        # at the DB layer, but parse_filters doesn't validate enum membership.
        result = parse_filters(
            QueryParams(f"status_in={values}"),
            allow_list=_games_allow_list(),
        )
        assert len(result["status"]["in"]) == 100

    def test_in_with_over_max_rejected(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        # Cap is 100; 101 should reject
        values = ",".join(f"v{i}" for i in range(101))
        with pytest.raises(QueryParamError, match=r"too many values|cap|100"):
            parse_filters(
                QueryParams(f"status_in={values}"),
                allow_list=_games_allow_list(),
            )

    async def test_via_http_returns_400(self, client, populated_pool):
        values = ",".join(f"v{i}" for i in range(101))
        r = await client.get(
            f"/api/v1/games?status_in={values}",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# S2-D: oversized int → 400 not 500
# ---------------------------------------------------------------------------


class TestS2DOversizedIntegerRejected:
    def test_signed_64bit_max_accepted(self):
        from orchestrator.api._query_helpers import parse_filters

        # 2^63 - 1 = max signed 64-bit; SQLite accepts
        result = parse_filters(
            QueryParams("size_bytes_gte=9223372036854775807"),
            allow_list=_games_allow_list(),
        )
        assert result["size_bytes"]["gte"] == 9223372036854775807

    def test_over_64bit_rejected(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        # 2^63 — one over signed 64-bit max; SQLite would OverflowError on bind
        with pytest.raises(QueryParamError, match=r"out of range|64-bit|too large"):
            parse_filters(
                QueryParams("size_bytes_gte=9223372036854775808"),
                allow_list=_games_allow_list(),
            )

    def test_under_64bit_min_rejected(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        # 2^63 + 1 negative — under signed 64-bit min
        with pytest.raises(QueryParamError, match=r"out of range|64-bit|too small|too large"):
            parse_filters(
                QueryParams("size_bytes_gte=-9223372036854775809"),
                allow_list=_games_allow_list(),
            )

    async def test_via_http_returns_400(self, client, populated_pool):
        r = await client.get(
            "/api/v1/games?size_bytes_gte=99999999999999999999999",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert r.status_code == 400
        assert (
            "out of range" in r.json()["detail"].lower()
            or "too large" in r.json()["detail"].lower()
        )


# ---------------------------------------------------------------------------
# S3-b + S3-c: build_*_clause defensive re-checks (symmetry + 400 not 500)
# ---------------------------------------------------------------------------


class TestS3BcBuildClauseDefensiveChecks:
    def test_build_order_by_rejects_unknown_field(self):
        """S3-b: build_order_by_clause must defensively re-validate field names
        even though parse_sort already validated. Future endpoint authors
        who hand-build SortFields can otherwise inject."""
        from orchestrator.api._query_helpers import (
            QueryParamError,
            SortAllowList,
            SortField,
            build_order_by_clause,
        )

        allow_list = SortAllowList(fields={"id", "title"})
        with pytest.raises(QueryParamError, match=r"not a sortable field|not allowed"):
            build_order_by_clause(
                [SortField(field="evil_field", direction="asc")],
                allow_list=allow_list,
            )

    def test_build_where_unknown_op_raises_query_param_error(self):
        """S3-c: build_where_clause must raise QueryParamError (-> 400),
        not KeyError (-> 500), when given an unknown op."""
        from orchestrator.api._query_helpers import QueryParamError, build_where_clause

        with pytest.raises(QueryParamError, match=r"unknown operator|not allowed"):
            build_where_clause(
                {"size_bytes": {"bogus_op": 100}},
                allow_list=_games_allow_list(),
            )


# ---------------------------------------------------------------------------
# S3-d + S3-e: metadata JSON parse hardening
# ---------------------------------------------------------------------------


class TestS3deMetadataParseHardening:
    async def test_oversized_metadata_returns_null(self, client, populated_pool):
        """S3-e: metadata > MAX_METADATA_BYTES short-circuits to null
        without invoking json.loads (defense against billion-laughs etc.)."""
        # Update one game's metadata to a giant string
        async with populated_pool.write_transaction() as tx:
            big_meta = '{"x":"' + ("y" * 200_000) + '"}'
            await tx.execute("UPDATE games SET metadata = ? WHERE id = 1", (big_meta,))
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        # Spec: parse-fail path returns null
        assert game["metadata"] is None

    async def test_deeply_nested_metadata_returns_null(self, client, populated_pool):
        """S3-d: deeply-nested JSON that triggers RecursionError on parse
        is caught and returns null, not 500."""
        # 2000 levels of nesting — python's default recursion limit is 1000
        deep = "[" * 2000 + "1" + "]" * 2000
        async with populated_pool.write_transaction() as tx:
            await tx.execute("UPDATE games SET metadata = ? WHERE id = 1", (deep,))
        r = await client.get(
            "/api/v1/games?limit=500",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        # Must not 500
        assert r.status_code == 200
        game = next(g for g in r.json()["games"] if g["id"] == 1)
        assert game["metadata"] is None


# ---------------------------------------------------------------------------
# S3-h: FilterAllowList rejects non-identifier field names
# ---------------------------------------------------------------------------


class TestS3hFilterAllowListIdentifierCheck:
    def test_invalid_identifier_rejected(self):
        from orchestrator.api._query_helpers import FilterAllowList, FilterFieldSpec

        # Field name with SQL syntax: must reject at construction
        with pytest.raises(ValueError, match=r"invalid identifier|must match"):
            FilterAllowList({"1=1 OR x": FilterFieldSpec(ops={"eq"}, value_type=str)})

    def test_valid_identifiers_accepted(self):
        from orchestrator.api._query_helpers import FilterAllowList, FilterFieldSpec

        # Standard SQL identifiers must work
        allow = FilterAllowList(
            {
                "platform": FilterFieldSpec(ops={"eq"}, value_type=str),
                "size_bytes": FilterFieldSpec(ops={"gte"}, value_type=int),
                "last_prefilled_at": FilterFieldSpec(ops={"gte"}, value_type=str),
            }
        )
        assert "platform" in allow.by_field

    def test_reserved_param_name_rejected(self):
        """S3-j: a field named `limit`/`offset`/`sort` is unreachable
        because the parser reserves those names. Must reject at construction."""
        from orchestrator.api._query_helpers import FilterAllowList, FilterFieldSpec

        for reserved in ("limit", "offset", "sort"):
            with pytest.raises(ValueError, match=r"reserved|cannot use"):
                FilterAllowList({reserved: FilterFieldSpec(ops={"eq"}, value_type=str)})


# ---------------------------------------------------------------------------
# S3-i: SortField/SortAllowList identifier validator
# ---------------------------------------------------------------------------


class TestS3iSortAllowListIdentifierCheck:
    def test_invalid_identifier_rejected(self):
        from orchestrator.api._query_helpers import SortAllowList

        with pytest.raises(ValueError, match=r"invalid identifier|must match"):
            SortAllowList(fields={"id; DROP TABLE games;"})


# ---------------------------------------------------------------------------
# S3-a: string-typed filter value content validation (timestamp format)
# ---------------------------------------------------------------------------


class TestS3aStringFilterValueValidation:
    """Note: this is a partial fix in scope. The full fix would add per-field
    validators (timestamp ISO format, etc.). For BL7's allow-list:
    - platform: enum (steam, epic) — validated by DB CHECK constraint downstream
    - status: enum (8 values) — same
    - last_prefilled_at / last_validated_at: ISO 8601 timestamps

    For UAT-4 we add basic ISO-format validation on timestamp fields.
    XSS-payload values to non-timestamp string fields would still pass through
    but never reach a render context (the orchestrator is the server; XSS is
    a downstream Game_shelf concern). Pin the timestamp validation here."""

    def test_timestamp_field_accepts_iso_8601(self):
        from orchestrator.api._query_helpers import parse_filters

        result = parse_filters(
            QueryParams("last_prefilled_at_gte=2026-05-01T00:00:00Z"),
            allow_list=_games_allow_list(),
        )
        assert result["last_prefilled_at"]["gte"] == "2026-05-01T00:00:00Z"

    def test_timestamp_field_accepts_date_only(self):
        from orchestrator.api._query_helpers import parse_filters

        # Date-only format also valid for SQLite text comparison
        result = parse_filters(
            QueryParams("last_prefilled_at_gte=2026-05-01"),
            allow_list=_games_allow_list(),
        )
        assert result["last_prefilled_at"]["gte"] == "2026-05-01"

    def test_timestamp_field_rejects_xss_payload(self):
        from orchestrator.api._query_helpers import QueryParamError, parse_filters

        with pytest.raises(QueryParamError, match=r"invalid|timestamp|format"):
            parse_filters(
                QueryParams("last_prefilled_at_gte=<script>alert(1)</script>"),
                allow_list=_games_allow_list(),
            )


# ---------------------------------------------------------------------------
# Helpers (mirror the games allow-list from BL7)
# ---------------------------------------------------------------------------


def _games_allow_list():
    from orchestrator.api._query_helpers import FilterAllowList, FilterFieldSpec

    return FilterAllowList(
        {
            "platform": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
            "status": FilterFieldSpec(ops={"eq", "in"}, value_type=str),
            "owned": FilterFieldSpec(ops={"eq"}, value_type=int),
            "size_bytes": FilterFieldSpec(ops={"eq", "gte", "lte"}, value_type=int),
            "last_prefilled_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
            "last_validated_at": FilterFieldSpec(ops={"gte", "lte"}, value_type="timestamp"),
        }
    )


def _games_sort_allow_list():
    from orchestrator.api._query_helpers import SortAllowList

    return SortAllowList(
        fields={"id", "title", "status", "size_bytes", "last_prefilled_at", "last_validated_at"}
    )
