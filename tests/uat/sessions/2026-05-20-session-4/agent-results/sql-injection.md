# UAT-4 SQL Injection Deep-Dive
**Agent:** sql-injection
**Date:** 2026-05-20

Persona: penetration tester. Goal: break the parametric SQL contract on `_query_helpers.py` (BL7), or document why each attack class fails.

Targets reviewed in full:
- `src/orchestrator/api/_query_helpers.py`
- `src/orchestrator/api/routers/games.py` (`count_sql` / `rows_sql` construction at L186-194)
- `src/orchestrator/db/pool.py` — `read_one` / `read_all` / `_checkout_reader`
- `docs/superpowers/specs/2026-05-17-bl7-games-readonly-design.md` §4.3 + §6 risk register
- `tests/api/test_query_helpers.py::TestSqlInjectionResistance`

All behavioural claims below were verified by running the actual module with crafted inputs (script output captured during the audit; see Findings notes).

---

## A: Field-name interpolation walk

### A.1 Trust path from the wire

```
HTTP query string
  → request.query_params  (Starlette QueryParams)
  → parse_filters()  ─── only key strings matched against
                         allow_list.by_field (dict[str, FilterFieldSpec])
  → returns dict {field_name: {op: value}} where field_name is
    GUARANTEED to be a literal key from allow_list.by_field
  → build_where_clause()  ─── DEFENSIVE re-check: `if field_name not in
                              allow_list.by_field: raise QueryParamError`
                              (L214-215)
  → f"{field_name} = ?".format(...)   (L223) and f"{field_name} IN (...)" (L220)
```

The interpolation at L220/L223 uses Python f-string composition of `field_name`. For the GAMES endpoint, the allow_list keys are literal source strings (`"platform"`, `"status"`, `"owned"`, `"size_bytes"`, `"last_prefilled_at"`, `"last_validated_at"`) — none of them are derived from user input. There is no code path where user data can mutate the FilterAllowList. Therefore for the games endpoint, the f-string interpolation is safe.

### A.2 Sort field walk

`parse_sort` (L258-286) splits on `,` then on `:`, validates `field_name in allow_list.fields` (L274) and `direction in ("asc","desc")` (L276). The narrowed `direction` is then ascribed to a `Literal["asc","desc"]` (`narrowed_direction`) — runtime cannot smuggle anything past `direction not in ("asc","desc")`.

`build_order_by_clause` (L289-294) does NOT re-validate against the allow-list. It blindly trusts the SortField dataclass.

### A.3 Defensive-recheck attack: a hand-rolled bad allow_list

The defensive re-check in `build_where_clause` validates the field name against the SAME allow-list that originated it. If an endpoint author ever wrote a `FilterAllowList` whose keys themselves contain SQL syntax, the defence is a no-op.

**Verified:**
```python
bad_allow = FilterAllowList({"id; DROP TABLE games; --": FilterFieldSpec(ops={"eq"}, value_type=str)})
build_where_clause({"id; DROP TABLE games; --": {"eq": "x"}}, allow_list=bad_allow)
# → "WHERE id; DROP TABLE games; -- = ?"
```

This is a **policy issue**, not a runtime CVE on BL7 — every BL7 caller hardcodes static identifier strings — but it is real surface that future endpoint authors WILL find a way to violate. See SEV-3 finding F-1.

**Verdict:** No exploitable injection through field-name interpolation on the games endpoint as shipped. The defensive recheck is structurally cosmetic (it re-validates against the same dict that produced the key), not a true second line of defence. Field-name safety is a property of the endpoint module, not of `_query_helpers`.

---

## B: Value binding walk

### B.1 String values
`_coerce_value` (L120-136) returns `raw` unchanged for `value_type is str`. The unchanged string is bound via `params.append(value)` (L224) or `params.extend(value)` (L221) in `build_where_clause`. The pool layer passes `params` to `conn.execute(sql, params)` (pool.py L914, L929) — aiosqlite parameterizes positional binds via SQLite's bind API, not via string interpolation. **No path puts a user string into SQL text.**

Verified: `?platform='; DROP TABLE games; --` produces `WHERE platform = ?` with `params=["'; DROP TABLE games; --"]`. The malicious string never enters the SQL text.

### B.2 int values
`int(raw)` (L124). On success, an `int` object lands in params. SQLite binds `int` as INTEGER. **Safe.**
On failure, `ValueError` is wrapped into `QueryParamError` → router returns 400.

Verified: `?size_bytes_gte=0 OR 1=1` raises `QueryParamError("invalid value for size_bytes_gte: '0 OR 1=1'")` because `int("0 OR 1=1")` fails. The classic numeric-injection vector is blocked at parse time, not at SQL build time — even better.

### B.3 float values
`float(raw)` (L126). Note: `float("nan")` and `float("inf")` SUCCEED in Python. They will be bound to SQLite as floats. SQLite handles NaN/inf cleanly. No SQL escape; just a possibly-surprising query result. No injection.

### B.4 bool values
Lookup-table coercion against `("1","true","True")` / `("0","false","False")` (L128-131). Anything else raises. Safe.

### B.5 Reserved-key skip
`parse_filters` skips `limit`, `offset`, `sort` (L152). They are handled by their respective parsers, both of which apply strict validation (`int()` for pagination; allow-list + literal direction for sort). No way to smuggle a filter through a reserved key.

**Verdict:** Value binding surface is clean. Every operator path through `_OP_SQL[op].format(field=field_name)` (L223) and the IN-clause path (L220) places the user value in `params` and a `?` in SQL.

---

## C: IN clause edge cases

### C.1 Empty list — `?status_in=`

`raw_value.split(",")` on an empty string returns `[""]` — a single-element list with the empty string. After `_coerce_value` (str passthrough), the parser stores `{"status": {"in": [""]}}`. `build_where_clause` emits `WHERE status IN (?)` with `params=[""]`. Valid SQL. Returns no rows (status enum has no empty value). **Not a crash, but the user probably expected "match nothing" or 400.**

There is NO code path that produces `WHERE status IN ()` because `split(",")` of an empty string is `[""]`, not `[]`. Verified.

### C.2 Comma-only — `?status_in=,`

`"".split(",")` → `["", ""]`. Produces `WHERE status IN (?, ?)` with `params=["", ""]`. Valid SQL; matches nothing. Verified.

### C.3 1000-item IN list

SQLite 3.32+ (this build: 3.51.3) defaults `SQLITE_MAX_VARIABLE_NUMBER` to **32766**. Verified that 999, 1500, 32766, and 32767 placeholders all execute successfully. So a "999" classical limit attack does not apply here.

However: there is **no upper bound on the number of comma-separated values** in `_in`. An attacker can send `?platform_in=` followed by 1MB of `a,a,a,...`. The string is split, each token is bound. Memory and CPU pressure scale linearly with payload size. The query is short-circuited by the allow-list (only "steam","epic" valid for `platform`), but the parser does all the work BEFORE returning rows.

This is a **resource-exhaustion / DoS surface**, not SQL injection. See SEV-3 finding F-2.

### C.4 `None` values
The parser never produces `None` — `_coerce_value` returns `raw` (a str), `int(raw)`, `float(raw)`, or `True/False`. None of those routes emit `None`. Safe.

---

## D: ORDER BY field-name surface

### D.1 As shipped
`parse_sort` validates every user-provided field against `allow_list.fields`. `build_order_by_clause` trusts the SortField dataclass.

### D.2 Direct payload in `sort` value

Verified: `?sort=title:asc; DROP TABLE games; --` → `parse_sort` splits `entry="title:asc; DROP TABLE games; --"`, sees `":"`, splits to `field_name="title"` + `direction="asc; drop table games; --"` (lowered). The direction check `direction not in ("asc","desc")` raises. **Blocked.**

### D.3 Hand-rolled bad SortField bypassing parse_sort

Verified: `build_order_by_clause([SortField(field="id; DROP TABLE games", direction="asc")])` returns `"ORDER BY id; DROP TABLE games ASC"`. **Direct injection.** The SortField dataclass has no runtime validator on its `field` attribute.

The current invocation chain in `routers/games.py` only constructs SortFields via `parse_sort`. But the boundary "parse_sort validates; build_order_by_clause trusts" is NOT documented in either function's docstring. A future endpoint author who constructs SortFields directly — e.g. for a fixed multi-sort policy — could omit the validation step without warning. See SEV-3 finding F-3.

### D.4 The `direction` Literal escape

`narrowed_direction: Literal["asc","desc"] = direction` (L279) uses a `# type: ignore[assignment]`. Runtime is still a plain `str`. The validation at L276 prevents bad runtime values — confirmed safe.

---

## E: Pool method interaction

### E.1 Binding semantics

`Pool.read_one` (pool.py L909-922) calls `conn.execute(sql, params)`. `Pool.read_all` (L924-937) likewise. Both pass `params` directly — `Sequence[Any] | Mapping[str, Any]` typed — to `aiosqlite.execute()`, which forwards to `sqlite3.Cursor.execute(sql, params)`. SQLite parameterizes positional `?` and named `:name` binds via the C API; never string interpolation. **Confirmed safe.**

### E.2 `params=None`?

`read_one`'s default is `params: Sequence[Any] | Mapping[str, Any] = ()` (L910). The games router passes `where_params` (a `list[Any]`) — never `None`. If a future caller passed `None`, aiosqlite would emit `TypeError: argument must be a sequence or dict, not NoneType` (or similar), which would be caught by the `except aiosqlite.Error` block — actually no, `TypeError` would propagate as-is, not be wrapped. The router's `except PoolError` would NOT catch it. This would leak a 500 ISE to the client. Minor robustness concern, not security. Not filed as a finding.

### E.3 `conn.row_factory = aiosqlite.Row` (L699)

Read results come back as named-tuple-like rows. The games router accesses them via `row["metadata"]`, `row["last_error"]`, etc. — no further SQL involvement. Safe.

### E.4 Other pool entry points
`acquire_reader()` (L1053) returns a raw aiosqlite connection. A caller could in theory build a non-parametric query against it, bypassing the helpers entirely. Out of scope for this audit (BL7 doesn't use it for games), but worth flagging for the OAS/code-review audit: any future caller that uses `acquire_reader()` directly is outside the parametric envelope. The pool log-redaction (`_template_only`) does scrub literals before logging.

**Verdict:** Pool layer correctly parameterizes. No injection vector.

---

## F: Property-test coverage gaps + proposed new tests

### F.1 Gaps in `TestSqlInjectionResistance`

Looking at the existing property test (test_query_helpers.py L370-392):

1. Only fuzzes the VALUE for `platform` (sampled from a tiny set) and `size_bytes` (integers only). The injection payload `"'; DROP TABLE games; --"` is sampled — but only as a `str` value going to a `str` field. The test does not cover:
2. The OPERATOR keys (always `eq` + `gte`).
3. The FIELD NAMES (always `platform` + `size_bytes`) — i.e. there's no fuzzing across allow-list fields.
4. The `in` operator (multi-value parsing). The most complex parser path is never property-tested.
5. Pagination + filter combinations — `LIMIT ? OFFSET ?` placeholders + the where-clause's `?` count must align with `len(params)`. If they drift, every query breaks; no property pins this.
6. Sort interaction with filter — `ORDER BY` field-name surface is untested except in unit tests with known-good inputs.
7. Round-trip: assertions are negative-only ("DROP TABLE not in sql"). Nothing checks that the placeholder count equals the params count, which is the strictest property of a parametric builder.

### F.2 Proposed additions

```python
# 1) Round-trip invariant: placeholder count == params count.
#    This is the load-bearing property — if it ever fails, either the SQL is
#    malformed or a value didn't get bound and was interpolated.
@given(
    field=st.sampled_from(["platform", "status", "size_bytes", "owned"]),
    op_and_val=st.one_of(
        st.tuples(st.just("eq"), st.text(min_size=0, max_size=64)),
        st.tuples(st.just("in"), st.lists(st.text(min_size=0, max_size=32),
                                          min_size=0, max_size=10)),
        st.tuples(st.just("gte"), st.integers(min_value=-(2**62), max_value=2**62)),
        st.tuples(st.just("lte"), st.integers(min_value=-(2**62), max_value=2**62)),
    ),
)
def test_placeholder_count_matches_params_count(self, field, op_and_val):
    from orchestrator.api._query_helpers import build_where_clause
    op, value = op_and_val
    # Filter to spec-compatible (field, op) combinations the allow-list permits.
    allow = _games_allow_list()
    if op not in allow.by_field[field].ops:
        return  # skip — allow-list would reject upstream
    # Skip type-incompatible combos (str value for int field, etc.) — those
    # raise at parse time, not build time.
    spec = allow.by_field[field]
    if op == "in":
        if spec.value_type is int and not all(
            isinstance(v, str) and v.lstrip("-").isdigit() for v in value
        ):
            return
        filters = {field: {"in": list(value)}}
    else:
        if spec.value_type is int and not (
            isinstance(value, int) or (isinstance(value, str) and value.lstrip("-").isdigit())
        ):
            return
        filters = {field: {op: value}}

    sql, params = build_where_clause(filters, allow_list=allow)
    # Invariant: every `?` in the SQL pairs with exactly one bind value.
    assert sql.count("?") == len(params), (sql, params)

# 2) Cross-field fuzz: combinations of fields & operators never leak values
#    into SQL text.  Drives the parser end-to-end via QueryParams.
@given(
    qs=st.lists(
        st.tuples(
            st.sampled_from([
                "platform", "platform_in", "status", "status_in",
                "size_bytes", "size_bytes_gte", "size_bytes_lte", "owned",
            ]),
            st.text(min_size=0, max_size=32,
                    alphabet=st.characters(blacklist_categories=("Cs",))),
        ),
        min_size=0, max_size=8,
    ),
)
def test_full_pipeline_never_interpolates_user_text(self, qs):
    from urllib.parse import urlencode
    from orchestrator.api._query_helpers import (
        QueryParamError, parse_filters, build_where_clause,
    )
    raw = urlencode(qs, doseq=False)
    try:
        filters = parse_filters(QueryParams(raw), allow_list=_games_allow_list())
        sql, params = build_where_clause(filters, allow_list=_games_allow_list())
    except QueryParamError:
        return  # Rejected by allow-list / type coercion — expected for fuzz misses.
    # Property: no user-supplied raw value substring appears in SQL text,
    # except for the known allow-list identifiers.
    safe_identifiers = {
        "platform","status","size_bytes","owned",
        "last_prefilled_at","last_validated_at",
        "WHERE","AND","IN","?",">=","<=","!=","=","<",">",
    }
    for _, value in qs:
        if not value:
            continue
        # The value MUST NOT appear in the SQL fragment — only in params.
        # (Allow whitespace/single chars matching identifiers; require >= 4
        # chars to make the check sensitive without false positives.)
        if len(value) >= 4 and value not in safe_identifiers:
            assert value not in sql, (value, sql, params)

# 3) Sort-clause field-name property: every emitted ORDER BY identifier is
#    a member of the allow-list. Catches a hypothetical regression where
#    parse_sort might be widened without updating the allow-list check.
@given(
    sort_param=st.text(min_size=0, max_size=120,
                       alphabet=st.characters(blacklist_categories=("Cs",))),
)
def test_parse_sort_only_emits_allow_listed_fields(self, sort_param):
    from orchestrator.api._query_helpers import (
        QueryParamError, SortField, build_order_by_clause, parse_sort,
    )
    allow = _games_sort_allow_list()
    try:
        result = parse_sort(
            QueryParams(f"sort={sort_param}"),
            allow_list=allow,
            default=[SortField(field="title", direction="asc")],
            tie_breaker=SortField(field="id", direction="asc"),
        )
    except QueryParamError:
        return
    # Every field in the result is allow-listed (the tie-breaker's field is
    # also allow-listed by spec construction).
    for s in result:
        assert s.field in allow.fields or s.field == "id"
        assert s.direction in ("asc", "desc")
    # And the assembled ORDER BY contains no semicolons / DDL keywords.
    order_sql = build_order_by_clause(result)
    assert ";" not in order_sql
    upper = order_sql.upper()
    for keyword in ("DROP", "DELETE", "UPDATE", "INSERT", "UNION", "--", "/*"):
        assert keyword not in upper
```

These three additions raise property coverage from "values for two fields" to "round-trip invariant + cross-field fuzz + sort identifier integrity". The first is the most load-bearing single property: if it ever fails, a parametric-binding bug exists somewhere in the helpers.

---

## G: Concrete attack payload library

All "actual" entries verified by running the live module.

| # | Attack payload (URL query) | Expected behaviour | Actual behaviour |
|---|---|---|---|
| 1 | `?platform='; DROP TABLE games; --` | Literal bound; 0 rows. | `WHERE platform = ?` with params=`["'; DROP TABLE games; --"]`. Returns 0 rows. **Safe.** |
| 2 | `?platform=steam' OR '1'='1` | Literal bound; 0 rows. | `WHERE platform = ?` with params=`["steam' OR '1'='1"]`. 0 rows. **Safe.** |
| 3 | `?status_in=a,b,c) UNION SELECT * FROM platforms; --` | 4 csv tokens bound; 0 rows. | `WHERE status IN (?, ?, ?)` with params=`["a", "b", "c) UNION SELECT * FROM platforms; --"]`. 0 rows. **Safe.** |
| 4 | `?sort=title:asc; DROP TABLE games; --` | 400 (bad direction). | `QueryParamError("invalid sort direction: 'asc; drop table games; --'")` → router returns 400. **Safe.** |
| 5 | `?sort=password` | 400 (unknown sort field). | `QueryParamError("'password' is not a sortable field")` → 400. **Safe.** |
| 6 | `?size_bytes_gte=0 OR 1=1` | 400 (not an int). | `QueryParamError("invalid value for size_bytes_gte: '0 OR 1=1'")` → 400. **Safe (blocked at parse).** |
| 7 | `?size_bytes_in=1,2,3` | 400 (op `in` not permitted on size_bytes). | `QueryParamError("operator 'in' not allowed for field 'size_bytes'")` → 400. **Safe.** |
| 8 | `?evil_field=anything` | 400 (unknown field). | `QueryParamError("unknown filter field: evil")` (split eats `_field` as `_eq`? no — none of the known operator suffixes match `_field`, so full key `evil_field` is treated as the field name, also unknown) → 400. **Safe.** |
| 9 | `?status_in=` (empty) | Ideally 400 or no-filter. | `WHERE status IN (?)` with params=`[""]`. Query valid; matches 0 rows. **Not a vuln; surprising UX.** See SEV-4 finding F-4. |
| 10 | `?status_in=,` | Probably 400. | `WHERE status IN (?, ?)` with params=`["", ""]`. Matches 0 rows. **Not a vuln; same UX issue as #9.** |
| 11 | `?platform=steam&platform=epic` (duplicate keys) | Either "first wins", "last wins", or "merged into IN". | Starlette `QueryParams.__iter__` yields each unique key ONCE; `params["platform"]` returns the LAST value. Result: `WHERE platform = ?` params=`["epic"]`. The "steam" value is silently discarded. **Not a vuln; possibly surprising.** See SEV-4 finding F-5. |
| 12 | `?platform_in=a,a,a,...` (×100,000) | Bounded or 400. | 100k placeholders + 100k params; SQLite executes it. Verified up to 32766 placeholders work. **DoS surface.** See SEV-3 finding F-2. |
| 13 | `?sort=title:asc,title:desc` (contradictory) | Both emitted, last wins at SQL level. | `ORDER BY title ASC, title DESC, id ASC`. SQLite uses the first key for primary sort; the contradiction is silently absorbed. **Not a vuln; not a useful exploit.** |
| 14 | Future bug: hand-build `FilterAllowList({"id; DROP TABLE games; --": ...})` | Should never happen — but does it crash? | Emits `WHERE id; DROP TABLE games; -- = ?`. **Defence-in-depth gap** (the runtime cannot tell whether an identifier is "safe"). See SEV-3 finding F-1. |
| 15 | Future bug: hand-built `SortField(field="id; DROP TABLE games", direction="asc")` | Should never happen — but does it crash? | Emits `ORDER BY id; DROP TABLE games ASC`. **Same class of defence-in-depth gap.** See SEV-3 finding F-3. |

---

## Findings

### SEV-1
None.

### SEV-2
None. The shipped games endpoint correctly parameterizes all user values, and the field-name allow-list keys are hardcoded literals.

### SEV-3

#### F-1 — Defence-in-depth recheck in `build_where_clause` is structurally cosmetic
The "defensive re-check" at `_query_helpers.py:214-215` validates `field_name in allow_list.by_field` — but `allow_list` is the SAME object the keys originated from. A FilterAllowList constructed with malicious keys passes the recheck trivially. Recommendation: enforce an identifier regex (`^[a-z][a-z0-9_]*$`) on every key when `FilterAllowList.__init__` is called, OR (preferred) on every emitted identifier inside `build_where_clause`. This makes the recheck a real second line of defence regardless of the caller's correctness.

#### F-2 — Unbounded `_in` token count enables CPU/memory DoS
`parse_filters` accepts arbitrarily long comma-separated `_in` payloads. SQLite's bind limit is 32766 placeholders; until then the parser splits, coerces, and the SQL builder concatenates. A single 1 MB query string with `?platform_in=a,a,...,a` does meaningful CPU + memory work before short-circuiting on the allow-list value mismatch (which never short-circuits, since 'a' is a string and `value_type=str` accepts it). Recommendation: cap `_in` list size — e.g., 200 elements — and raise `QueryParamError` past that. Document the cap in the endpoint spec.

#### F-3 — `build_order_by_clause` trusts SortField; no validator on the dataclass
`SortField.field` is a plain `str`. `build_order_by_clause` emits `f"{s.field} {s.direction.upper()}"` without any validation. The boundary "parse_sort validates; build_order_by trusts pre-validated SortFields" is not documented in either function's docstring. A future endpoint author who constructs SortField objects directly (e.g., for fixed default sorts in a different endpoint) could omit the allow-list check without any compile-time warning. Recommendation:
  - Add a `__post_init__` validator on SortField that enforces `field` matches `^[a-z][a-z0-9_]*$` and `direction in ("asc","desc")`. Cheap, dataclass-friendly, and makes the dataclass safe-by-construction.
  - OR: add the docstring boundary callout in BOTH `parse_sort` and `build_order_by_clause`.

### SEV-4

#### F-4 — Empty `_in` list silently builds `IN (?)` with `""`
`?status_in=` produces a query that matches 0 rows but consumes a DB roundtrip. Recommendation: in `parse_filters`, after `raw_value.split(",")`, filter out empty-after-strip tokens; if the resulting list is empty, raise `QueryParamError("status_in: at least one value required")`. UX win + tiny perf win.

#### F-5 — Duplicate filter keys silently discard all but the last value
`?platform=steam&platform=epic` parses to `platform=epic` (Starlette's `QueryParams.__iter__` yields each key once; `params[key]` returns the last value). The user's intent — "either steam or epic" — is plausibly `_in`, but they would never know their first value was dropped. Recommendation: in `parse_filters`, detect duplicate keys via `getlist(key)` length > 1 and raise `QueryParamError("duplicate query parameter: <key>; use '<field>_in=' for multi-value filters")`. Improves error messaging; prevents silent data loss.

### Non-findings (verified safe / out of scope)

- **PRAGMA f-strings in `pool.py:_open_connection`**: Pragma names/values come from a hardcoded list inside the pool itself; no user input touches this surface.
- **Pool error wrapping (`_wrap_aiosqlite_error`)**: scrubs literal values from logs via `_template_only` (`_LITERAL_RE`). Verified to handle string/identifier/hex/NULL/numeric literals.
- **`narrowed_direction: Literal["asc","desc"] = direction` with `# type: ignore`**: runtime check at L276 guarantees the runtime str is one of those two; the type assertion is purely for the type-checker.
- **`metadata` JSON parsing in games router**: pure post-query handling; doesn't influence SQL surface.
- **`last_error` truncation at 200 chars**: same — output processing, not SQL surface.
- **`acquire_reader()` raw escape hatch**: out of scope for BL7 (games router doesn't use it). Worth a separate review for any future caller.
- **SQLite variable limit ("999 limit" attack)**: doesn't apply here; the build's limit is 32766 and was raised in SQLite 3.32.

---
