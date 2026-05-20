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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
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
        raise QueryParamError(f"limit must be >= 1, got {limit}")
    if limit > max_limit:
        raise QueryParamError(f"limit must be <= {max_limit}, got {limit}")
    if offset < 0:
        raise QueryParamError(f"offset must be >= 0, got {offset}")

    return PaginationParams(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


# Operator -> SQL fragment ("?" placeholder for value). Field names
# interpolated AFTER allow-list validation. "in" handled specially.
_OP_SQL = {
    "eq": "{field} = ?",
    "ne": "{field} != ?",
    "gte": "{field} >= ?",
    "lte": "{field} <= ?",
    "gt": "{field} > ?",
    "lt": "{field} < ?",
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
        # frozen dataclass with non-default __init__ — use object.__setattr__
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
        raise QueryParamError(f"invalid value for {field_name}_{op}: {raw!r}") from e


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
    reserved = {"limit", "offset", "sort"}
    result: dict[str, dict[str, Any]] = {}

    for key in params:
        if key in reserved:
            continue

        # Field + operator parse. Try each known suffix; the first match wins.
        # Suffixes are checked longest-first to avoid "_in" eating "_int" if
        # a future operator name has that shape.
        field_name = key
        op = "eq"
        if "_" in key:
            for candidate_op in ("gte", "lte", "gt", "lt", "ne", "in", "eq"):
                suffix = f"_{candidate_op}"
                if key.endswith(suffix):
                    field_name = key[: -len(suffix)]
                    op = candidate_op
                    break

        if field_name not in allow_list.by_field:
            raise QueryParamError(f"unknown filter field: {field_name}")
        spec = allow_list.by_field[field_name]
        if op not in spec.ops:
            raise QueryParamError(f"operator {op!r} not allowed for field {field_name!r}")

        raw_value = params[key]
        if op == "in":
            values = [
                _coerce_value(v.strip(), spec.value_type, field_name, op)
                for v in raw_value.split(",")
            ]
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
                raise QueryParamError(f"{field_name!r} is not a sortable field")
            if direction not in ("asc", "desc"):
                raise QueryParamError(f"invalid sort direction: {direction!r}")
            # Narrow str → Literal for type checker after runtime validation
            narrowed_direction: Literal["asc", "desc"] = direction  # type: ignore[assignment]
            user_sort.append(SortField(field=field_name, direction=narrowed_direction))

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
