# UAT-4 Input Validation Fuzz
**Agent:** input-validation
**Date:** 2026-05-20
**Scope:** `src/orchestrator/api/_query_helpers.py` + consumer `routers/games.py`
**Method:** Code-trace fuzz against BL7 spec (no live execution against the app).

The BL7 helpers lock conventions reused by every future paginated endpoint
(`/jobs`, `/manifests`, `/stats`, `/block_list`), so any gap here is a
multiplier across F9.

---

## Vector A — Filter keys with multi-underscore field names

**Parser shape (lines 162–170):**
```python
field_name = key
op = "eq"
if "_" in key:
    for candidate_op in ("gte", "lte", "gt", "lt", "ne", "in", "eq"):
        suffix = f"_{candidate_op}"
        if key.endswith(suffix):
            field_name = key[: -len(suffix)]
            op = candidate_op
            break
```

The tuple is roughly longest-first (`gte`/`lte` are 3 chars and listed
first); the remaining 2-char ops (`gt`, `lt`, `ne`, `in`, `eq`) are
mutually exclusive endings, so order among them doesn't matter for
unambiguous suffixes.

| Input key | Expected | Actual | Gap? |
|---|---|---|---|
| `created_at_gte=...` (future field `created_at`) | field=`created_at`, op=`gte` | field=`created_at`, op=`gte` (suffix `_gte` matches first) | None |
| `created_at_in=a,b` | field=`created_at`, op=`in` | field=`created_at`, op=`in` (suffixes `_gte`/`_lte`/`_gt`/`_lt`/`_ne` miss, `_in` matches) | None |
| `created_at_gt=...` | field=`created_at`, op=`gt` | field=`created_at`, op=`gt` (longest-suffix avoids eating `_gte` because string doesn't end with it) | None |
| `created_at=...` (no op) | field=`created_at`, op=`eq` | Loop sees `_at` is NOT in the suffix vocabulary so falls through; *but* `_eq` is in the vocabulary too — string doesn't end with `_eq`, so `op` stays default `eq`. field=`created_at`. | None |
| `created_in=...` (hypothetical field `created_in` doesn't exist) | unknown filter field error referencing `created`, op=`in` | field=`created`, op=`in` → `created` not in allow_list → 400 `unknown filter field: created` | **Minor UX gap (SEV-4)** — the error message names the *stripped* field, which can confuse an operator who typed `created_in` thinking it was a field. |
| Hypothetical field literally named `foo_gte` (no operator desired) | accessible via `?foo_gte=v` for eq | Parser strips `_gte` → field=`foo`, op=`gte` → 400 unless `foo` is also allow-listed. Field `foo_gte` is **unaddressable for eq** (any direct GET will be reinterpreted as op-suffix). User would have to use `foo_gte_eq` which strips to `foo_gte` only if `_eq` runs first — but `_gte` matches first because it's longer in the loop order, so `foo_gte_eq` → field=`foo_gte`, op=`eq` (correct because `_gte` doesn't match end of `foo_gte_eq`). | **SEV-4 design note**: documented convention in BL7 spec §4.1 doesn't call out that future endpoint authors **MUST NOT** declare a field whose name ends in any of `_eq/_in/_gte/_lte/_gt/_lt/_ne` for direct (no-suffix) access. Spec §3.1 implicitly enforces this by listing fields, but a guard in `FilterAllowList.__init__` would make it explicit. |
| Future field `foo_gte_in` (op `in`) | field=`foo_gte`, op=`in` | `_gte` doesn't match end (key ends `_in`); `_in` matches → field=`foo_gte`, op=`in`. Works **iff** `foo_gte` is allow-listed. | None — but reinforces SEV-4 above. |

**Regression test sketch:**
```python
def test_field_name_ending_in_op_suffix_reserved():
    """Future field names ending in _gte/_lte/_gt/_lt/_ne/_in/_eq
    cannot be addressed without a suffix — document or guard."""
    # Add a paranoid validator to FilterAllowList that raises
    # ValueError on field names with reserved suffix endings.
```

---

## Vector B — Adversarial filter values

| Input | Expected | Actual (per code) | Gap? |
|---|---|---|---|
| `?platform_in=steam,epic,a%2Cb` | 3 values (escaped comma `a,b` as one) **or** 4 values (no escape supported, documented) | Starlette URL-decodes `%2C` to `,` **before** `parse_filters` sees it; `raw_value.split(",")` yields `["steam","epic","a","b"]` — **4 values**. No comma-escape mechanism. | **SEV-3** — spec §3.1 silent on escapes. A platform identifier or status enum containing a comma cannot be sent. Acceptable for current enum-only `_in` use (status/platform), but locks the contract: every future `_in` field MUST NOT contain a comma in any legal value, OR a quoting convention must be added before that ships. Recommend: document explicitly in spec §3.1 and add a regression test asserting current behaviour. |
| `?status_in=` (empty value) | Reject 400 OR ignore | `raw_value=""`, `"".split(",") → [""]`, coerced as `str` → `[""]` → WHERE `status IN (?)` with `[""]` → returns no rows. **Silently accepted, semantically dead.** | **SEV-3** — operator sees empty result, no error. Likely typo / link-with-trailing-equals. Fix: reject empty `_in` value in `parse_filters` (raise `QueryParamError("empty value list for ... _in")`). |
| `?status_in=not_downloaded` (single value with `_in`) | 1-element list | `["not_downloaded"]` → `WHERE status IN (?)` → equivalent to `status=not_downloaded`. Works fine. | None |
| `?status_in=a,b,` (trailing comma) | 2 values? Or 3 incl `""`? | `split(",") → ["a","b",""]` → 3 values, last is empty string. WHERE `status IN (?,?,?)` with `["a","b",""]`. Returns rows matching `a`, `b`, or empty-string status (none). | **SEV-3** — silent extra empty-string match. Fix: filter out empty post-strip in the `_in` comprehension, **and** raise if the post-filter list is empty. |
| `?size_bytes_gte=00100` | 100 (coerced) | `int("00100")` → 100. Accepted. | None — Python `int()` strips leading zeros for base-10. Acceptable. |
| `?size_bytes_gte=-1` | Accepted; query returns all rows (size ≥ -1) | Accepted; `int("-1")` → -1; valid bind. Spec §3.1 doesn't constrain negative ints for size_bytes. Returns all rows with size_bytes IS NOT NULL. | **SEV-4 design note** — negative size_bytes is nonsensical for the domain but spec doesn't forbid. Not a vulnerability, just lazy. Optional: per-field `min_value` in `FilterFieldSpec`. |
| `?size_bytes_gte=100.5` | Reject 400 (int field, float value) | `int("100.5")` → ValueError → caught → `QueryParamError("invalid value for size_bytes_gte: '100.5'")` → 400. | None — correct. |
| `?size_bytes_gte=9999999999999999999999999` | Reject 400 OR clamp | `int(...)` succeeds (Python big-int). Coerce returns 25-digit int. SQLite `INTEGER` is signed 64-bit; binding via `sqlite3` raises **`OverflowError: Python int too large to convert to SQLite INTEGER`** at `pool.read_one`/`read_all`. This is **NOT** a `PoolError` subclass — it will propagate as an uncaught exception unless `Pool` wraps it. | **SEV-2 (verify)** — depends on `Pool.read_one`/`read_all` exception handling. If it doesn't catch `OverflowError` and re-raise as `PoolError`, the operator sees a 500 with traceback (worst case via FastAPI default error). **Action:** verify in `src/orchestrator/db/pool.py` and either (a) catch OverflowError in pool → PoolError → 503, or (b) validate range in `_coerce_value` (preferred — proper 400 with clear message). |
| `?platform=stéam` (unicode in str field) | Passed through to SQL params; no match | `str` path of `_coerce_value` returns raw. SQLite TEXT binds accept UTF-8. Returns no rows. No injection risk (parameterized). | None — safe. |

**Regression test sketches:**
```python
def test_in_with_empty_value_raises():
    with pytest.raises(QueryParamError, match="empty"):
        parse_filters(QueryParams("status_in="), allow_list=_games_allow_list())

def test_in_with_trailing_comma_rejected_or_filtered():
    # Either drop the empty trailing entry OR raise — pick one and lock.
    ...

def test_oversized_int_returns_400_not_500():
    # ?size_bytes_gte=99...9 (25 digits) must produce 400, not 500.
    r = await client.get("/api/v1/games?size_bytes_gte=" + "9"*25, headers=AUTH)
    assert r.status_code == 400
```

---

## Vector C — Sort param edge cases

| Input | Expected | Actual | Gap? |
|---|---|---|---|
| `?sort=` (empty) | Default + tie-breaker | `params.get("sort")` returns `""`, `not raw` is True → uses `default` → `[title:asc, id:asc]` | None |
| `?sort=,,` (just commas, no fields) | Default + tie-breaker OR 400 | `raw=",,"` is truthy, so default branch is **skipped**. Loop splits to `["", "", ""]`, each stripped to `""`, `continue`. `user_sort = []`. Then tie-breaker appended → `[id:asc]` only. **Default sort is NOT applied.** | **SEV-2** — empty-after-stripping sort silently drops the default `title:asc` ordering. Result: a 50-row page comes back ordered only by `id:asc` instead of the documented `title:asc` default. This is a **silent UX correctness bug** for any client that hits a typo'd sort param. Fix: after the loop, if `user_sort` is empty (i.e., raw was non-empty but contained only commas), either apply `default` OR raise `QueryParamError("sort param empty")`. |
| `?sort=title:` (colon, no direction) | 400 | `":" in entry` True → `direction=""` → not in `("asc","desc")` → 400 `"invalid sort direction: ''"` | None — correct, though message says `''` which is slightly cryptic. |
| `?sort=:asc` (no field) | 400 | `field_name=""`, `direction="asc"` → `""` not in allow_list → 400 `"'' is not a sortable field"` | None — correct, message slightly cryptic. |
| `?sort=title:asc:extra` (extra colon) | 400 | `split(":", 1)` → field=`"title"`, direction=`"asc:extra"` → `.lower()` → `"asc:extra"` → not in `("asc","desc")` → 400. | None — correct. |
| `?sort=title:ASC` | Accepted (lowercased) | `.strip().lower() → "asc"` → accepted. | None |
| `?sort=title&sort=size_bytes` (duplicate keys) | Documented behaviour (first wins / last wins / error) | Starlette's `params.get("sort")` returns the **first** occurrence. Second silently ignored. | **SEV-3** — silent param shadowing. Operator sending two `sort=` params gets only the first. Fix: explicitly check `len(params.getlist("sort")) <= 1` and raise on duplicate. Same applies to duplicate `limit` and `offset`. |
| `?sort=title:asc,title:desc` (same field twice) | Documented behaviour | Both parsed; emits `ORDER BY title ASC, title DESC, id ASC` — SQL-legal but the `DESC` is a no-op (rows already ordered by ASC). | **SEV-4** — surprising behaviour, not a bug per se. Recommend: deduplicate by `field` (first occurrence wins) in `parse_sort`, OR raise on duplicate field. |

**Regression test sketches:**
```python
def test_sort_only_commas_applies_default_or_raises():
    """Decide and lock: ?sort=,,, must either apply default or raise — not
    silently drop default."""
    result = parse_sort(QueryParams("sort=,,,"), allow_list=..., default=[SortField("title","asc")], tie_breaker=SortField("id","asc"))
    # If chosen behaviour = apply default:
    assert result[0].field == "title"

def test_duplicate_sort_params_rejected_or_first_wins_explicit():
    # Lock behaviour and document.
    ...

def test_duplicate_sort_field_dedupes_or_raises():
    ...
```

---

## Vector D — Pagination boundaries

| Input | Expected | Actual | Gap? |
|---|---|---|---|
| `?limit=0` | 400 | `int("0") → 0`, `limit < 1` → `QueryParamError("limit must be >= 1, got 0")`. | None — covered by `TestParsePagination::test_zero_limit_raises`. |
| `?limit=` (empty value) | 400 (consistent with `=abc`) | `params.get("limit")` returns `""` (not None). `int("")` → ValueError → `QueryParamError`. | None — correct. |
| `?limit=10&limit=20` (duplicate) | Documented | First wins (Starlette behaviour, same as sort). | **SEV-3** — same as duplicate sort. Silent. Fix: explicit duplicate-rejection. |
| `?offset=99999999999` (huge offset, no overflow) | Accepted, returns empty page | `int(...)` fine, bind fine, SQLite returns 0 rows. Total still correct. | None — but no upper bound on offset means an operator can `offset=2**63-1` and still hit DB. No DoS surface because COUNT is cheap. |
| `?offset=2**70` (above sqlite int range) | 400 OR 503 | Python `int(...)` accepts, SQLite binding will likely `OverflowError`. Same gap as oversized size_bytes. | **SEV-3** — same OverflowError → 500 risk as B above. Fix: cap offset at `2**63 - 1` in `parse_pagination`. |
| Big request size: `?limit=500&offset=0`, then `?limit=500&offset=500` | Both succeed independently | Two stateless requests. No issue at app layer. | None |

---

## Vector E — Reserved param namespace collisions

**Current reserved set (line 152):** `{"limit", "offset", "sort"}`.

| Scenario | Expected | Actual | Gap? |
|---|---|---|---|
| Future field literally named `limit` | Either reject in `FilterAllowList.__init__` OR document conflict | `parse_filters` does `if key in reserved: continue` — silently skips. The field is **unreachable as a filter**. | **SEV-3 design lock** — convention is unstated. Future endpoint author declaring a field `limit` (e.g., a `block_list.limit` column) will have a silently broken filter. Fix: `FilterAllowList.__init__` raises `ValueError` if any field name is in `{limit, offset, sort}`. |
| `applied_filters` vs `where_builder` drift | Should be identical | Both walk the same `filters` dict; `build_where_clause` does its own allow-list re-check but never silently drops a field — if an unknown field were in the dict, it raises. So the echo and the WHERE **cannot diverge** given current code. | None |
| `parse_filters` returns op `in` for known field but `build_where_clause` doesn't have it in `_OP_SQL` | `in` is handled in a special branch, not via `_OP_SQL`, so missing-key error impossible. If a future op (e.g., `like`) is added to a `FilterFieldSpec.ops` but not to `_OP_SQL` and not to the `in` special branch, `build_where_clause` raises `KeyError` (unhandled — would 500). | **SEV-3 hardening** — `_OP_SQL` is the single source of truth for SQL emission. Add a startup-time invariant: every op in any `FilterFieldSpec.ops` must be in `_OP_SQL` or be `"in"`. Either assert in `FilterAllowList.__init__` or add a unit test that asserts the union-set invariant. |

---

## Vector F — Case sensitivity matrix

| Input | Expected | Actual | Gap? |
|---|---|---|---|
| `?Platform=steam` | 400 (allow-list is case-sensitive) | `"Platform" not in allow_list.by_field` → 400 `"unknown filter field: Platform"`. | None |
| `?size_bytes_GTE=100` | 400 (suffix case-sensitive) | `key.endswith("_gte")` is `False` (Python `endswith` is case-sensitive). No suffix matches. field=`size_bytes_GTE`, op=`eq`. `size_bytes_GTE` not in allow_list → 400 `"unknown filter field: size_bytes_GTE"`. | **SEV-4 UX** — error message says "unknown filter field" rather than hinting that the operator suffix is case-sensitive. Operator may chase the wrong fix. Recommend: detect the case-insensitive suffix match and emit a hint. |
| `?sort=title:Asc` | Accepted (`.lower()` in code) | `direction.strip().lower() → "asc"` → accepted. | None — correct. |
| `?sort=Title:asc` | 400 (field case-sensitive) | `"Title" not in allow_list.fields` → 400. | None — correct, same UX caveat as above. |

---

## Vector G — applied_filters echo round-trip

| Scenario | Expected | Actual | Gap? |
|---|---|---|---|
| `?status_in=a,b` → echo key for IN | `"in"` (wire alias) | Router builds `crit_kwargs["in_"]=...`; `FilterCriterion` has `in_: ... = Field(alias="in")` + `populate_by_name=True`; serialized with `body.model_dump(by_alias=True)` (router line 262). Result: JSON key is `"in"`. | None — verified. |
| `?status=foo&status_in=bar` (both eq AND in on one field) | Open: spec doesn't say | Both parsed. `result["status"] = {"eq":"foo", "in":["bar"]}` → WHERE emits `status = ? AND status IN (?)` (ANDed). Echo: `{"status":{"eq":"foo","in":["bar"]}}`. Returns rows where `status=foo AND status IN [bar]` — empty unless foo==bar. | **SEV-4 design ambiguity** — spec §3.1 says "within a field, multiple criteria AND together" so this IS the documented behaviour. Acceptable. Recommend: a router-level test asserting this explicitly, since "both ops on one field" is a footgun. |
| No filter params → `applied_filters` | `{}` empty dict OR absent | Router builds `applied_filters: dict[str, FilterCriterion] = {}` and passes it directly. JSON serializes as `{}`. Always present (not absent). | None — consistent with envelope contract `extra="forbid"` + `applied_filters: dict[...]`. |
| `FilterCriterion(extra="forbid")` round-trip | Any unknown op key on input → 422 | Input is server-built so never adversarial. Output `extra="forbid"` only fires if router code adds an unknown key to `crit_kwargs`. Current code only adds keys from `parse_filters` output which is allow-list-bound. Safe. | None |

---

## Findings (numbered)

### SEV-2 — Sort with only commas silently drops default ordering

**Scenario:** `GET /api/v1/games?sort=,,,` returns 50 rows ordered by `id ASC`
only (tie-breaker), **not** by the documented default `title:asc`. The page is
documented to be alphabetised; it isn't.

**Affected code:** `_query_helpers.py:258–286` `parse_sort`. The branch
`if not raw:` evaluates `False` for `,,,` (non-empty string), so `default` is
skipped, but the loop yields no entries, leaving `user_sort = []`.

**Fix (minimal):**
```python
raw = params.get("sort")
if not raw or not raw.strip(", "):
    user_sort = list(default)
else:
    user_sort = []
    for entry in raw.split(","):
        ...
    if not user_sort:  # all entries were empty after strip
        user_sort = list(default)
```
Or raise `QueryParamError("sort: no non-empty entries")` — loud is better.

**Regression test:**
```python
def test_sort_only_commas_applies_default():
    result = parse_sort(
        QueryParams("sort=,,,"),
        allow_list=_games_sort_allow_list(),
        default=[SortField("title","asc")],
        tie_breaker=SortField("id","asc"),
    )
    assert result == [SortField("title","asc"), SortField("id","asc")]
```

---

### SEV-2 — Oversized integer in numeric filter / offset triggers 500

**Scenario:** `GET /api/v1/games?size_bytes_gte=9999999999999999999999999`
(25 digits). Python `int(...)` succeeds, but `sqlite3` cannot bind a value
outside signed-64-bit range and raises `OverflowError: Python int too large
to convert to SQLite INTEGER` at the pool layer.

**Affected code:** `_query_helpers.py:120–137` `_coerce_value` — accepts any
Python int. The error surfaces inside `pool.read_one/read_all`. Whether the
user sees 500 vs 503 depends on `db/pool.py`'s exception coverage (verify).
Same vector via `?offset=2**70` and any future int filter field.

**Fix:** Add a range check in `_coerce_value` for `int` (cap at signed-64-bit:
`-(2**63) <= v <= 2**63 - 1`) and raise `QueryParamError` with proper 400.
Defence-in-depth: pool wraps `OverflowError` → `PoolError`.

**Regression test:**
```python
async def test_oversized_int_returns_400_not_500(client, games_pool_100):
    r = await client.get(
        "/api/v1/games?size_bytes_gte=" + "9" * 25,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 400
    assert "value" in r.json()["detail"].lower()
```

---

### SEV-3 — Empty `_in` value and trailing-comma `_in` silently accepted

**Scenario A:** `?status_in=` → parsed as `status IN ('')` → returns no rows
(no error). Operator sees an empty page from what they likely intended as
"no filter" (link with trailing `=`).
**Scenario B:** `?status_in=a,b,` → parsed as `status IN ('a','b','')` →
includes a phantom empty-string match.

**Affected code:** `_query_helpers.py:179–184` `parse_filters` `in` branch.

**Fix:** After comprehension, drop empty strings, and raise
`QueryParamError("empty value list for {field}_in")` if the resulting list
is empty.

**Regression tests:**
```python
def test_in_empty_raises():
    with pytest.raises(QueryParamError, match="empty"):
        parse_filters(QueryParams("status_in="), allow_list=_games_allow_list())

def test_in_trailing_comma_drops_empty():
    result = parse_filters(QueryParams("status_in=a,b,"), allow_list=_games_allow_list())
    assert result == {"status": {"in": ["a", "b"]}}
```

---

### SEV-3 — Duplicate query params (`limit`, `offset`, `sort`) silently shadow

**Scenario:** `?sort=title&sort=size_bytes` — Starlette's `.get()` returns
only the first; the second is silently dropped. Same for `limit` and
`offset`. Operator gets behaviour they didn't request and no error.

**Affected code:** `_query_helpers.py:55–56` (pagination), `:258` (sort) —
all use `.get(...)` instead of `.getlist(...)`.

**Fix:** For each reserved/sort param, check
`if len(params.getlist(name)) > 1: raise QueryParamError(...)`.

**Regression test:**
```python
async def test_duplicate_sort_param_returns_400(client, games_pool_100):
    r = await client.get(
        "/api/v1/games?sort=title&sort=size_bytes",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r.status_code == 400
```

---

### SEV-3 — Comma-escape semantics undocumented in `_in` values

**Scenario:** `?platform_in=a%2Cb` (URL-encoded comma `a,b`) is decoded by
Starlette to `a,b` before `parse_filters`, so it splits as two values. There
is no escape mechanism. Currently invisible because all real `_in` enums
contain no commas — but the BL7 spec **locks the convention** for all
future `_in` fields.

**Affected code:** `_query_helpers.py:182` — `raw_value.split(",")` is
unconditional.

**Fix (choose one):**
1. Document in spec §3.1: "Values in `_in` lists must not contain commas. No
   escape mechanism." Add a `FilterAllowList` doctring warning.
2. Add a quoting convention (e.g., backslash-escape or alternate separator
   via `_in_sep=|`). More work; YAGNI for current enum-only `_in` usage.

**Recommend option 1** + add an assertion in code that detects
URL-decoded-comma corner cases if needed.

---

### SEV-3 — Reserved param namespace silently swallows colliding field names

**Scenario:** If a future endpoint's allow-list declares a field literally
named `limit`, `offset`, or `sort`, `parse_filters` will skip the param
silently (line 152, `if key in reserved: continue`). The field is
effectively unreachable as a filter.

**Affected code:** `_query_helpers.py:152` reserved set; no validation in
`FilterAllowList.__init__`.

**Fix:** `FilterAllowList.__init__` raises if any allow-listed field name
collides with the reserved set:
```python
RESERVED_PARAM_NAMES = frozenset({"limit", "offset", "sort"})
def __init__(self, by_field):
    bad = RESERVED_PARAM_NAMES & by_field.keys()
    if bad:
        raise ValueError(f"filter field names cannot use reserved param names: {sorted(bad)}")
    object.__setattr__(self, "by_field", dict(by_field))
```

**Regression test:**
```python
def test_filter_allow_list_rejects_reserved_names():
    with pytest.raises(ValueError, match="reserved"):
        FilterAllowList({"limit": FilterFieldSpec(ops={"eq"}, value_type=int)})
```

---

### SEV-3 — Op-set / `_OP_SQL` consistency not asserted

**Scenario:** A future endpoint author adds `"like"` to
`FilterFieldSpec.ops` but forgets to extend `_OP_SQL`. At runtime
`build_where_clause` raises `KeyError("like")` → 500.

**Affected code:** `_query_helpers.py:91–98` `_OP_SQL`; no startup invariant.

**Fix:** `FilterAllowList.__init__` checks
`spec.ops ⊆ _OP_SQL.keys() | {"in"}` for every spec.

**Regression test:**
```python
def test_filter_allow_list_rejects_unsupported_op():
    with pytest.raises(ValueError, match="op"):
        FilterAllowList({"x": FilterFieldSpec(ops={"like"}, value_type=str)})
```

---

### SEV-4 — Sort with same field twice produces redundant ORDER BY

**Scenario:** `?sort=title:asc,title:desc` → `ORDER BY title ASC, title DESC, id ASC`.
SQL-legal, but the second clause is a no-op and the user clearly didn't
mean it.

**Fix:** Either dedupe-by-field (first wins) or raise on duplicate.

---

### SEV-4 — Negative `size_bytes` accepted

**Scenario:** `?size_bytes_gte=-1` accepted; semantically meaningless for
the domain. Not a security issue.

**Fix (optional):** Add `min_value` to `FilterFieldSpec` for int fields,
or document as "garbage in, empty out".

---

### SEV-4 — Field-name suffix collision with operator suffixes (design lock)

**Scenario:** Any future field whose name ends in `_eq`/`_in`/`_gte`/`_lte`/
`_gt`/`_lt`/`_ne` is unreachable for the default-eq path. See vector A
matrix.

**Fix:** Add a validator in `FilterAllowList.__init__` that warns or rejects
such names, and document the rule in `_query_helpers.py` docstring.

---

### SEV-4 — Case-insensitive operator suffix produces misleading error

**Scenario:** `?size_bytes_GTE=100` → "unknown filter field: size_bytes_GTE".
Operator may chase the wrong fix (think the field is renamed) instead of
realising operator suffixes are case-sensitive.

**Fix:** In the error path, attempt a case-insensitive suffix match; if it
would match, emit "operator suffixes are case-sensitive — did you mean
`size_bytes_gte`?".

---

## Non-findings (explicitly safe)

- **SQL injection via filter values** — values flow exclusively through
  `?` placeholders. Hypothesis property test
  `TestSqlInjectionResistance::test_build_where_never_interpolates_values`
  asserts this. Verified by reading `build_where_clause` (line 223 uses
  `.format(field=field_name)` for the *field* — already allow-list-validated
  — and appends the *value* to `params` via `?`).
- **SQL injection via field name** — `field_name` is interpolated into the
  SQL string but **only** after `if field_name not in allow_list.by_field`
  rejection. Defensive re-check in `build_where_clause` (line 214) makes the
  invariant layered. Sort fields same pattern at `build_order_by_clause`.
- **Unicode in str field values** — passes through cleanly via SQLite
  parameter binding; UTF-8 round-trips. No normalization issue at the API
  layer (DB will compare bytewise).
- **Float coerce for int field** — properly raises `QueryParamError`. Same
  for `?owned=true` or non-numeric int.
- **`limit=0` and `offset<0`** — both correctly rejected with explicit
  error messages.
- **`FilterCriterion` alias `in`** — `populate_by_name=True` + router
  `model_dump(by_alias=True)` ensure JSON wire key is `"in"`, not `"in_"`.
- **Empty `applied_filters`** — emits `{}` always (consistent envelope).
- **Multiple criteria on same field (eq + in)** — documented "AND within
  field" semantics in spec §3.1; behaviour matches.
- **Multiple sort params** — *behaviour* is "first wins"; this is
  documentable but listed as SEV-3 because it's currently silent.
- **`sort=` empty value** — correctly applies default (because `not raw`
  evaluates True for `""`). Only the `,,,`-style case is broken (SEV-2).
- **Case-sensitive field allow-list** — `Platform` properly rejected.
- **Sort direction case** — properly lowercased; `Asc` / `DESC` /
  `dEsC` all accepted.

---

## Recommendation summary

Before BL7 ships and these conventions propagate to `/jobs`, `/manifests`,
`/stats`, `/block_list`:

1. **Fix the SEV-2s** (sort-only-commas; oversized int → 500). These are
   correctness bugs visible to the operator.
2. **Lock the SEV-3 design decisions in code, not in spec prose**: add the
   `FilterAllowList.__init__` invariants for reserved names + op-vs-SQL
   consistency. Reject duplicate params explicitly. Reject empty `_in`
   lists.
3. **Document SEV-4s** (field-name suffix collision; comma escape policy;
   case-sensitivity hints) in the `_query_helpers.py` module docstring so
   the next endpoint author can't miss them.

All proposed fixes are additive — no behavioural change for the BL7 happy
paths in `tests/api/test_games_router.py`. They tighten boundaries that are
currently undefined or silently accepted.
