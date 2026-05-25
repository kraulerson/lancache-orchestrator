"""Shared query primitives for paginated F9 read endpoints (BL7+).

This module's strict scope is parser + validator + SQL builder ONLY.
No domain logic; no endpoint-specific knowledge. Endpoints (routers/games.py,
future routers/jobs.py, etc.) construct their own FilterAllowList and
SortAllowList values describing what their endpoint permits, then compose
the helpers below to parse incoming Query params and produce parameterized
SQL fragments.

Design conventions locked in spec 2026-05-17-bl7-games-readonly-design.md
and revised in UAT-4 (2026-05-20):

- Offset-based pagination (limit/offset query params)
- Operator-suffix filter syntax: field, field_in, field_gte, field_lte,
  field_gt, field_lt, field_ne (per-endpoint allow-list narrows to subset)
- Multi-field sort: ?sort=a:desc,b:asc; empty entries are skipped and if
  the user-sort ends up empty the default applies (UAT-4 S2-B fix).
- Server-appended tie-breaker (with de-dup if user already sorted by it)
- `_in` lists capped at MAX_IN_VALUES per request (UAT-4 S2-C)
- Integer values must fit signed 64-bit (UAT-4 S2-D)
- FilterAllowList / SortAllowList field names must be valid SQL
  identifiers and must not collide with reserved param names
  (UAT-4 S3-h, S3-i, S3-j)
- FilterFieldSpec.validator (optional) is called after coercion to enforce
  per-field content rules (e.g., timestamp ISO format) (UAT-4 S3-a)

Security invariants:
- User values flow EXCLUSIVELY through SQL parameter binds (never f-string
  or %-formatted into the SQL text). Field names ARE interpolated into SQL
  but only AFTER allow-list validation guarantees they're safe identifiers
  from the endpoint's declared field set.
- Both build_where_clause AND build_order_by_clause defensively re-validate
  field names against the allow-list (UAT-4 S3-b).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.datastructures import QueryParams


# Module constants
MAX_IN_VALUES = 100  # UAT-4 S2-C
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
# Issue #86.D4: canonical name for the per-row error-truncation cap shared
# across platforms/games/jobs routers. Was previously 3 separate constants
# (`_LAST_ERROR_TRUNCATE`, `LAST_ERROR_TRUNCATE`, `ERROR_TRUNCATE`).
ERROR_TRUNCATE_BYTES = 200
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_RESERVED_PARAM_NAMES = frozenset({"limit", "offset", "sort", "include"})
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


# Issue #86.D2: shared Pydantic response model for sort meta. Was
# duplicated across games.py / jobs.py / manifests.py; OpenAPI happens to
# dedupe but any future divergence breaks the schema.
class SortFieldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    direction: Literal["asc", "desc"]


class QueryParamError(ValueError):
    """Raised when a query param fails parse/validation. The router catches
    this and returns 400 with the error message as `detail`."""


# ---------------------------------------------------------------------------
# Identifier validation (UAT-4 S3-h, S3-i, S3-j)
# ---------------------------------------------------------------------------


def _validate_identifier(name: str, *, kind: str) -> None:
    """Validate a SQL identifier used as a field name.

    Rejects anything that isn't lowercase snake_case ASCII. The orchestrator's
    schema uses this convention; tightening to it lets us interpolate field
    names into SQL with confidence. Reserved param names (limit/offset/sort)
    are also rejected because the filter parser would silently swallow them.
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"{kind}: {name!r} is not a valid identifier (must match ^[a-z_][a-z0-9_]*$)"
        )
    if name in _RESERVED_PARAM_NAMES:
        raise ValueError(
            f"{kind}: {name!r} is reserved (cannot use limit/offset/sort as a field name)"
        )


# ---------------------------------------------------------------------------
# Value validators (UAT-4 S3-a)
# ---------------------------------------------------------------------------


def _validate_timestamp_string(value: str) -> None:
    """Validate that a string looks like an ISO 8601 date or datetime.

    Accepts:
    - YYYY-MM-DD
    - YYYY-MM-DDTHH:MM:SS
    - YYYY-MM-DDTHH:MM:SS.fff
    - With optional Z or ±HH:MM timezone
    """
    if not _TIMESTAMP_RE.match(value):
        raise ValueError(
            f"invalid timestamp format: {value!r} (expected ISO 8601 date or datetime)"
        )
    # Also try a strict parse so e.g. month=13 is rejected
    try:
        # Handle "Z" suffix (datetime.fromisoformat in <3.11 doesn't)
        normalized = value.rstrip("Z")
        if "T" in normalized or " " in normalized:
            datetime.fromisoformat(normalized.replace(" ", "T"))
        else:
            datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"invalid timestamp value: {value!r} ({e})") from e


# Sentinel-string value_types used at the spec layer to distinguish
# str-valued fields that need extra validation. Callers pass either a real
# Python type (str/int/float/bool) OR the string "timestamp".
_VALUE_TYPE_VALIDATORS: dict[str, Callable[[str], None]] = {
    "timestamp": _validate_timestamp_string,
}


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
    if offset > INT64_MAX:
        raise QueryParamError(f"offset out of range (signed 64-bit), got {offset}")

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


# Acceptable value_type specifiers: real Python types OR the string
# "timestamp" (extensible at the validator dispatch layer).
ValueTypeSpec = type | str


@dataclass(frozen=True)
class FilterFieldSpec:
    """Declarative spec for one filter field: allowed ops + Python type
    (or sentinel string for typed-string variants like "timestamp")."""

    ops: set[str]  # subset of {"eq", "in", "gte", "lte", "gt", "lt", "ne"}
    value_type: ValueTypeSpec  # str, int, float, bool, or "timestamp"


@dataclass(frozen=True)
class FilterAllowList:
    """Per-endpoint declaration of permitted filter fields and operators.

    UAT-4 S3-h, S3-j: validates field names at construction. Field names
    must be valid SQL identifiers and must not collide with reserved
    param names (limit, offset, sort).
    """

    by_field: dict[str, FilterFieldSpec] = field(default_factory=dict)

    def __init__(self, by_field: dict[str, FilterFieldSpec]) -> None:
        for name in by_field:
            _validate_identifier(name, kind="filter field")
        # Validate ops against known _OP_SQL keys (+ "in")
        known_ops = set(_OP_SQL.keys()) | {"in"}
        for name, spec in by_field.items():
            unknown = spec.ops - known_ops
            if unknown:
                raise ValueError(f"filter field {name!r}: unknown operators {unknown!r}")
        # frozen dataclass with non-default __init__ — use object.__setattr__
        object.__setattr__(self, "by_field", dict(by_field))


def _coerce_value(raw: str, value_type: ValueTypeSpec, field_name: str, op: str) -> Any:
    """Coerce a string query-param value to the spec'd Python type, then
    apply per-type validation (UAT-4 S2-D int range, S3-a timestamp format).
    """
    try:
        if value_type is int:
            coerced = int(raw)
            if not (INT64_MIN <= coerced <= INT64_MAX):
                raise ValueError(f"value out of range (signed 64-bit): {coerced}")
            return coerced
        if value_type is float:
            coerced_f = float(raw)
            # UAT-5 U5-4: stdlib float() accepts "NaN"/"Infinity"; json.dumps then
            # raises ValueError ("out of range") → 500. Reject non-finite values
            # at the parse boundary so they become a 400 instead.
            if not math.isfinite(coerced_f):
                raise ValueError(f"value must be finite: {coerced_f}")
            return coerced_f
        if value_type is bool:
            if raw in ("1", "true", "True"):
                return True
            if raw in ("0", "false", "False"):
                return False
            raise ValueError(f"not boolean: {raw!r}")
        if isinstance(value_type, str):
            # Typed-string variant (e.g., "timestamp")
            validator = _VALUE_TYPE_VALIDATORS.get(value_type)
            if validator is None:
                raise ValueError(f"unknown typed-string value_type: {value_type!r}")
            validator(raw)
            return raw
        # default: plain str
        return raw
    except (TypeError, ValueError) as e:
        raise QueryParamError(f"invalid value for {field_name}_{op}: {raw!r} ({e})") from e


def parse_filters(
    params: QueryParams,
    *,
    allow_list: FilterAllowList,
) -> dict[str, dict[str, Any]]:
    """Parse query params into a `{field: {op: value}}` structure.

    Raises QueryParamError on unknown field, unknown op, value-type
    mismatch, or `_in` list exceeding MAX_IN_VALUES (UAT-4 S2-C).
    Returns empty dict if no filter params present.

    Reserved param names (limit, offset, sort) are skipped; they belong to
    pagination/sort, not filters.
    """
    result: dict[str, dict[str, Any]] = {}

    for key in params:
        if key in _RESERVED_PARAM_NAMES:
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
            raw_values = raw_value.split(",")
            if len(raw_values) > MAX_IN_VALUES:
                raise QueryParamError(
                    f"too many values for {field_name}_in: {len(raw_values)} (cap: {MAX_IN_VALUES})"
                )
            values = [_coerce_value(v.strip(), spec.value_type, field_name, op) for v in raw_values]
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

    UAT-4 S3-c: raises QueryParamError (→ 400) on unknown op, not KeyError
    (→ 500). The defensive re-check on field names against the allow_list
    is a layered invariant — even if a caller assembles the filters dict
    by hand, we won't interpolate a wild identifier.
    """
    if not filters:
        return "", []

    fragments: list[str] = []
    params: list[Any] = []

    for field_name, ops in filters.items():
        if field_name not in allow_list.by_field:
            raise QueryParamError(f"unknown filter field: {field_name}")

        for op, value in ops.items():
            if op == "in":
                placeholders = ", ".join("?" for _ in value)
                fragments.append(f"{field_name} IN ({placeholders})")
                params.extend(value)
            elif op in _OP_SQL:
                fragments.append(_OP_SQL[op].format(field=field_name))
                params.append(value)
            else:
                raise QueryParamError(f"unknown operator {op!r} for field {field_name!r}")

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
    """UAT-4 S3-i: validates field names at construction."""

    fields: set[str] = field(default_factory=set)

    def __init__(self, fields: set[str]) -> None:
        for name in fields:
            _validate_identifier(name, kind="sort field")
        object.__setattr__(self, "fields", set(fields))


@dataclass(frozen=True)
class IncludeAllowList:
    """Per-endpoint declaration of permitted ?include= expansion keys.

    BL9: opt-in FK expansion convention. Each endpoint declares
    which include keys are allowed (e.g., {"game"} for /manifests).
    Identifier-validated at construction.
    """

    keys: frozenset[str] = field(default_factory=frozenset)

    def __init__(self, keys: set[str] | frozenset[str]) -> None:
        for k in keys:
            _validate_identifier(k, kind="include key")
        object.__setattr__(self, "keys", frozenset(keys))


def parse_sort(
    params: QueryParams,
    *,
    allow_list: SortAllowList,
    default: list[SortField],
    tie_breaker: SortField,
) -> list[SortField]:
    """Parse `sort` query param into a list of `SortField` entries.

    Empty/absent sort applies `default`. UAT-4 S2-B: if the user supplies
    a sort string but all entries are empty after stripping (e.g., `,,,`),
    the default also applies — silently dropping the default would be a
    bug. Server-appends `tie_breaker` unless the user's sort already orders
    by `tie_breaker.field` (in either direction) — the user's explicit
    ordering wins.

    Issue #86.D1: user-supplied sort entries are deduped by field name
    (first occurrence wins). Prevents `?sort=id:asc,id:desc` from
    producing `ORDER BY id ASC, id DESC` (which works but is nonsense).
    """
    raw = params.get("sort")
    user_sort: list[SortField] = []
    seen_fields: set[str] = set()
    if raw:
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
            # Issue #86.D1: skip duplicate field references — first wins
            if field_name in seen_fields:
                continue
            seen_fields.add(field_name)
            # Narrow str → Literal for type checker after runtime validation
            narrowed_direction: Literal["asc", "desc"] = direction  # type: ignore[assignment]
            user_sort.append(SortField(field=field_name, direction=narrowed_direction))

    # UAT-4 S2-B: if parse produced nothing (empty raw OR all entries empty),
    # apply default. Previously, non-empty-but-all-empty-entries silently
    # produced only the tie-breaker.
    if not user_sort:
        user_sort = list(default)

    # Append tie_breaker if not already sorting by its field
    if not any(s.field == tie_breaker.field for s in user_sort):
        user_sort.append(tie_breaker)

    return user_sort


def build_order_by_clause(
    sort: list[SortField],
    *,
    allow_list: SortAllowList | None = None,
) -> str:
    """Build the `ORDER BY ...` SQL fragment from validated sort spec.

    UAT-4 S3-b: when `allow_list` is provided, defensively re-validate
    every field against it before interpolating into SQL. Callers that
    construct SortField hand (i.e., not via parse_sort) get the same
    safety net as build_where_clause already had.
    """
    if not sort:
        return ""
    if allow_list is not None:
        for s in sort:
            if s.field not in allow_list.fields:
                raise QueryParamError(f"{s.field!r} is not a sortable field (not allowed)")
    entries = [f"{s.field} {s.direction.upper()}" for s in sort]
    return "ORDER BY " + ", ".join(entries)


# ---------------------------------------------------------------------------
# Includes (BL9: opt-in FK expansion via ?include=)
# ---------------------------------------------------------------------------


def parse_includes(
    params: QueryParams,
    *,
    allow_list: IncludeAllowList,
) -> set[str]:
    """Parse ?include= query param into a deduplicated set of include keys.

    Empty/absent ?include= returns an empty set. Unknown keys raise
    QueryParamError. Values are comma-separated; per-key whitespace
    stripped; deduplicated; empty values dropped.

    The `include` query param is reserved (see _RESERVED_PARAM_NAMES) so
    the filter parser will not interpret it as a filter field.
    """
    raw = params.get("include")
    if not raw:
        return set()
    requested = {k.strip() for k in raw.split(",") if k.strip()}
    unknown = requested - allow_list.keys
    if unknown:
        raise QueryParamError(f"include keys not allowed: {sorted(unknown)}")
    return requested
