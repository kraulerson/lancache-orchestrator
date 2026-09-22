# UAT Session 17 — Exploratory (Malicious User)

**Scope:** `tools/cache_catcher/` (kuma.py, key_budget.py, key_budget_probe.py,
fanotify_guard.py), migration `0018_retire_failed_status.sql`, the single-writer
guard (`tests/test_measurement_writer_guard.py`).

**Method:** code reading + throwaway probe scripts under `/tmp/kb_probe` and
inline heredocs, run with the project's `.venv`. No source, test, or config file
was modified. No live system was touched.

**Verdict:** One SEV-1 (a corrupted history file can make the keys_zone alarm
report "up" when the cache is actually full or already over floor — the exact
failure class this alarm was built to prevent), one SEV-2 (kuma.push can raise
despite a "never raises" docstring and a test asserting it doesn't, on an input
class the test doesn't cover), two SEV-3 (a misleading "ceiling unknown"
message when the ceiling is a real, computed zero; the single-writer guard is
defeated by syntactically valid SQL it never anticipated), and three SEV-4
documentation/robustness gaps. Migration 0018's ordering, atomicity, and
idempotency claims all held up under independent verification — see the "Held"
section.

## Findings table

| # | Component | Finding | Severity |
|---|---|---|---|
| 1 | `key_budget.project_days_to` / `verdict` | A `nan` value in the history CSV silently poisons the trend projection and flips the verdict to `"up"` | SEV-1 |
| 2 | `kuma.push` | `msg=None` (or any object without `len()`) raises `TypeError` outside the `try` block, violating the documented and tested "never raises" contract | SEV-2 |
| 3 | `key_budget.effective_capacity` | `if k` truthiness test discards a legitimately-computed ceiling of exactly 0 and reports "unknown" with a false explanation | SEV-3 |
| 4 | `tests/test_measurement_writer_guard.py` | Regex scan is defeated by syntactically valid SQL (schema-qualified table, table alias) and by any runtime-built table name | SEV-3 |
| 5 | `kuma.push` | A push URL that already carries a query string produces a malformed double-`?` URL | SEV-4 |
| 6 | `key_budget.verdict` | `Sample.stderr` (computed, can be `inf`) is never surfaced in the verdict message | SEV-4 |
| 7 | `key_budget.ram_capacity_keys` | A negative `RAM_BUDGET_BYTES` (operator typo) is not rejected and produces a negative ceiling that always wins `min()` | SEV-4 |

---

## Finding 1 — NaN in history produces a false "up" verdict (SEV-1)

**File:** `tools/cache_catcher/key_budget.py` (`project_days_to`, `verdict`),
fed by `tools/cache_catcher/key_budget_probe.py` (`read_history`).

`read_history` parses each CSV field with `float(parts[0])` / `float(parts[1])`
and only catches `ValueError`. Python's `float()` accepts the literal strings
`"nan"`, `"inf"`, `"-inf"`, `"infinity"` (case-insensitive) as valid floats —
they do **not** raise `ValueError`, so a corrupted or hand-edited row
containing the string `nan` is accepted as legitimate data, not skipped like a
genuinely non-numeric field (`"abc"`, which *is* correctly skipped).

That `nan` flows into `project_days_to`, which computes
`per_day = (n1 - n0) / (elapsed / 86400.0)`. If `n0` (or `n1`) is `nan`,
`per_day` is `nan`. `nan <= 0` is `False` in IEEE 754, so the function falls
through the "shrinking/flat" guard and returns `(target - n1) / per_day`,
which is also `nan` — it does **not** return `None` as it does for the other
three explicitly-guarded bad cases (too few points, flat trend, shrinking
trend).

In `verdict`, `days` is now `nan`. `days is not None` is `True` (nan is not
None), so the code does not fall into "trend unknown"; it formats it as
`f"{days:.0f}d to floor"` → the literal string `"nand to floor"`. Then
`days < horizon_days` is `False` (any comparison against NaN is False), so the
`"FILLING FAST"` branch is skipped and the function falls through to
`return Verdict("up", detail)` — a clean "up" (healthy) verdict, at 50% of
ceiling in the repro below, with no visible sign of a fault beyond an odd
`"nand"` substring buried in the message that nothing consumes programmatically
(Kuma just displays it as text).

This exactly inverts the module's own stated design intent
(`project_days_to`'s docstring: *"None covers three cases that must never be
reported as reassurance... the most dangerous possible output"*) — the NaN
case is a fourth one that was missed, and it's worse than the three that were
guarded, because it doesn't even fall back to "trend unknown"; it falls all
the way to "up".

**How `nan` gets into the file for real:** the CSV lives at `/log/key_budget.csv`
on a NAS this project has already had NFS-staleness and mass-deletion
incidents on (see project memory). A partial/interrupted write, a manual edit,
or a future bug that ever writes a `NaN` `sample.objects` (e.g. from a
divide-by-zero elsewhere) would all produce exactly this string. No code path
anywhere in `key_budget.py` or `key_budget_probe.py` calls `math.isfinite()`
on parsed or computed values.

**Repro:**
```
$ PATH="$PWD/.venv/bin:$PATH" .venv/bin/python3
>>> import sys; sys.path.insert(0, "tools/cache_catcher")
>>> import key_budget as kb
>>> history = [(1000.0, float('nan')), (2000.0, 5_000_000.0)]
>>> kb.project_days_to(history, target=10_000_000.0)
nan
>>> sample = kb.Sample(objects=5_000_000.0, stderr=1000.0, leaves_read=256, read_failures=0)
>>> kb.verdict(sample, kb.Ceiling(keys=10_000_000, name="zone"), history, floor=0.75, horizon_days=90.0)
Verdict(status='up', msg='5.0M objects, 50% of zone ceiling 10.0M, nand to floor')
```

Also confirmed the ingestion path end to end — a CSV line containing the
literal string `nan` round-trips through `read_history` as valid data:
```
$ printf '1758000300,nan\n' >> /tmp/kb_probe/hist1.csv
>>> import key_budget_probe as kbp
>>> kbp.read_history("/tmp/kb_probe/hist1.csv")
[(1758000000.0, 1000000.0), (1758000300.0, nan)]
```
(Other corruption forms in the same file — a line with no comma, a line with
only one field, a non-numeric second field (`"abc"`), and a truncated final
line with no trailing newline — were all correctly skipped. Only the
`nan`/`inf`/`-inf` literals get through, because they're valid `float()`
input.)

**Fix shape (not implemented, per scope):** `read_history` should reject a
row where either parsed value is not `math.isfinite()`, treating it the same
as a `ValueError`.

---

## Finding 2 — `kuma.push` can raise on a non-string/None `msg` (SEV-2)

**File:** `tools/cache_catcher/kuma.py`

The docstring is explicit: *"Send one heartbeat. Never raises."* — and
`tests/tools/test_kuma.py` has a test named
`test_a_connection_failure_reports_undelivered_and_never_raises`. But the
contract is only enforced for failures during the network call, which is
inside the `try`. The line before it is not:

```python
trimmed = msg[-MSG_MAX_CHARS:] if len(msg) > MSG_MAX_CHARS else msg
```

`len(msg)` is called before the `try` block starts. If `msg` is `None` or an
`int` (anything without `__len__`), this raises `TypeError` uncaught.

**Repro:**
```
$ PATH="$PWD/.venv/bin:$PATH" .venv/bin/python3
>>> import sys; sys.path.insert(0, "tools/cache_catcher")
>>> import kuma
>>> kuma.push("http://example.com/push", "up", msg=None)
Traceback (most recent call last):
  ...
TypeError: object of type 'NoneType' has no len()
>>> kuma.push("http://example.com/push", "up", msg=12345)
Traceback (most recent call last):
  ...
TypeError: object of type 'int' has no len()
```

**Reachability today:** every current call site (`key_budget_probe.py` lines
174/189, `fanotify_guard.py` lines 184/355/365/371) passes an already-formatted
`str` (an f-string, a `%`-formatted string, or `result.msg` which is always a
`str` per `Verdict`'s `NamedTuple` field type). So this is **not** an active
incident — it's a latent defect in code that exists specifically as the last
line of defense against the monitored process crashing. `msg=bytes` (also
tested) does *not* raise — `len()` and `urllib.parse.quote_via` both accept
`bytes` — only `None`/non-sized types do.

Notably, `fanotify_guard.liveness_loop` (the guard's OWN liveness heartbeat, on
a 15-minute clock, with no restart supervisor for its daemon thread) has *no*
`try/except` around its `kuma.push` calls at all — unlike `probe_loop`, which
wraps `run_once()` in `try/except` and explicitly pushes `"down"` on failure.
If any future change to `kuma.push` (or a change to what gets passed as `msg`)
ever makes it raise, `liveness_loop` dies silently and permanently, and
`KUMA_PUSH_CACHE_GUARD` goes silent — which the file's own comments say is
"the correct signal" for a dead guard, so the blast radius is bounded, but the
loop never recovers on its own.

**Fix shape:** move the slicing/encoding into the `try`, or coerce `msg` to
`str(msg)` up front.

---

## Finding 3 — `effective_capacity`'s truthiness test hides a real ceiling of 0 (SEV-3)

**File:** `tools/cache_catcher/key_budget.py`

```python
known = [(k, name) for k, name in ((zone_keys, "zone"), (ram_keys, "ram")) if k]
```

`if k` treats `0` the same as `None` — but `ram_capacity_keys` **can**
legitimately return `0`: `int(ram_budget_bytes / bytes_per_key)` rounds down
to `0` whenever the configured RAM budget is smaller than the byte cost of a
single key (plausible on this exact deployment — live `bytes_per_key` was
measured at 128.5, and issue #346 already documents the zone being
provisioned far beyond what the host's RAM can back). When that happens,
`effective_capacity` drops the `0` and returns `Ceiling(None, "unknown")`
instead of `Ceiling(0, "ram")`.

**Consequence:** `verdict()` still reports `status="down"` (because
`ceiling.keys is None` is its own down-trigger), so the alarm does not go
silent — but the message it emits is actively false:

```
ceiling unknown (CACHE_INDEX_SIZE and RAM both unreadable); 5.0M objects
```

RAM was not unreadable — it was read, computed, and came out to exactly zero,
which is the single most urgent value this module can produce (zero room
left). The message sends an operator debugging at 3am toward "check why
`/proc` reads are failing" instead of "the RAM budget is undersized," which is
exactly the failure this module's own docstring says it exists to prevent
(*"A monitor that reports a number without saying what the number is bounded
by cannot tell the operator what to do about it"* — #326/#330).

**Repro:**
```
$ PATH="$PWD/.venv/bin:$PATH" .venv/bin/python3
>>> import key_budget as kb
>>> kb.ram_capacity_keys(ram_budget_bytes=100, bytes_per_key=1000)
0
>>> kb.effective_capacity(zone_keys=None, ram_keys=0)
Ceiling(keys=None, name='unknown')
>>> sample = kb.Sample(objects=5_000_000.0, stderr=1000.0, leaves_read=256, read_failures=0)
>>> kb.verdict(sample, kb.effective_capacity(None, 0), history=[])
Verdict(status='down', msg='ceiling unknown (CACHE_INDEX_SIZE and RAM both unreadable); 5.0M objects')
```

**Fix shape:** `if k is not None` instead of `if k`.

---

## Finding 4 — Single-writer guard is defeated by valid SQL it doesn't parse (SEV-3)

**File:** `tests/test_measurement_writer_guard.py`

The guard is a source-level regex scan, by explicit design (the docstring says
so), which is a reasonable trade-off — but its two patterns both anchor on the
literal token sequence `UPDATE`/`games`/`SET` (or `INTO`/`games`/`(`) appearing
adjacent in the source text, separated only by `\s+`. Any syntactically valid
SQL that puts something other than whitespace between those tokens evades it
completely, while still executing as a genuine write to `games.status`.

Tested against the guard's own `writes_cache_truth()` function, then verified
each SQL form is real, executable SQLite against a live `games` table:

| Evasion | Caught by guard? | Valid SQLite, writes `status`? |
|---|---|---|
| `UPDATE main.games SET status=?, status_measured_at=... WHERE id=?` (schema-qualified table name) | **No** | **Yes** — confirmed |
| `UPDATE games AS g SET status=? WHERE g.id=?` (table alias) | **No** | **Yes** — confirmed |
| `INSERT INTO main.games (id, status) VALUES (?, ?)` (schema-qualified INSERT) | **No** | **Yes** — confirmed |
| `"UPDATE " + "games" + " SET status=?..."` (runtime `+` concatenation) | **No** | n/a — not a fixed literal; would produce the identical working SQL string at runtime |
| `"UPDATE {} SET status=?...".format("games")` | **No** | n/a — same reasoning |
| `f"UPDATE {TABLE} SET status=?..."` (table name from a variable) | **No** | n/a — same reasoning |

**Repro (guard evasion):**
```
$ PATH="$PWD/.venv/bin:$PATH" .venv/bin/python3
>>> import importlib.util
>>> spec = importlib.util.spec_from_file_location("guard", "tests/test_measurement_writer_guard.py")
>>> guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
>>> guard.writes_cache_truth('"UPDATE main.games SET status=?, status_measured_at=CURRENT_TIMESTAMP WHERE id=?"')
False
>>> guard.writes_cache_truth('"UPDATE games AS g SET status=?, status_measured_at=CURRENT_TIMESTAMP WHERE g.id=?"')
False
>>> guard.writes_cache_truth('"INSERT INTO main.games (platform, app_id, status) VALUES (?, ?, ?)"')
False
```

**Repro (SQL validity — the alias/schema forms actually write the column):**
```
$ PATH="$PWD/.venv/bin:$PATH" .venv/bin/python3
>>> import sqlite3
>>> conn = sqlite3.connect(":memory:")
>>> conn.execute("CREATE TABLE games (id INTEGER PRIMARY KEY, status TEXT, status_measured_at TEXT)")
>>> conn.execute("INSERT INTO games (id, status) VALUES (1, 'unknown')")
>>> conn.execute("UPDATE main.games SET status='up_to_date' WHERE id=1")
>>> conn.execute("SELECT status FROM games WHERE id=1").fetchone()
('up_to_date',)
>>> conn.execute("UPDATE games AS g SET status='up_to_date' WHERE g.id=1")   # after resetting to 'unknown'
>>> conn.execute("SELECT status FROM games WHERE id=1").fetchone()
('up_to_date',)
```
(I also tried a bare alias with no `AS` keyword — `UPDATE games g SET ...` —
which SQLite's `UPDATE` grammar rejects, `near "g": syntax error`, unlike
`SELECT`. That form is not a real evasion; the `AS g` and schema-qualified
forms are.)

This is the shape asked for, not a patch: any helper that builds an `UPDATE
games` statement generically (a shared "update by dict of columns" utility
parameterizing the table name, or simply a developer adding `AS g`/`main.` out
of habit) writes real cache truth completely invisibly to this guard, with no
warning and no test failure. Given issue #312 already documents nine known
evasions, this is very plausibly a tenth+eleventh+twelfth class not yet on
that list. I did not check #312's exact text (not in scope/not fetched), so
there is some chance of overlap with an already-known evasion; either way, all
six forms above are independently reproduced as live misses today.

---

## Finding 5 — `kuma.push` mishandles a URL with an existing query string (SEV-4)

**File:** `tools/cache_catcher/kuma.py`

```python
target = f"{url.strip()}?{query}"
```

unconditionally appends `?...`, with no check for whether `url` already
contains a `?`. If it does, the result has two `?` characters, which is not a
well-formed URL — the URL RFC treats only the first `?` as the query
delimiter, so everything after the first `?status=...` clause (i.e. the whole
second query string) becomes part of the *value* of whatever key preceded it,
or is dropped/misparsed depending on the receiving server.

**Repro:**
```python
>>> import kuma
>>> class FakeResp:
...     status = 200
...     def __enter__(self): return self
...     def __exit__(self, *a): return False
>>> captured = {}
>>> def fake_opener(target, timeout=None):
...     captured['target'] = target
...     return FakeResp()
>>> kuma.push("http://example.com/push?existing=1", "up", msg="hi", opener=fake_opener)
True
>>> captured['target']
'http://example.com/push?existing=1?status=up&msg=hi'
```

**Reachability:** low. `KUMA_PUSH_*` values come from `/log/keybudget.env` /
`/log/alert.env` and are Uptime Kuma push tokens, which are plain
`.../api/push/<token>` URLs with no query string by convention. Flagged as a
documentation gap / defense-in-depth miss, not an active bug, since nothing in
this codebase currently configures a push URL with a query string.

---

## Finding 6 — `Sample.stderr` is computed but never surfaced (SEV-4, "surprising but correct")

**File:** `tools/cache_catcher/key_budget.py`

`summarise_sample` computes a real standard error (`inf` for `n==1`, shrinking
as `n` grows), but `verdict()` never reads `sample.stderr` — it's dead weight
downstream of where it's produced. A misconfigured `SAMPLE_LEAVES=1` (or a
sample where 255 of 256 leaves fail to read, leaving `leaves_read=1`) produces
a verdict message that looks exactly as confident as a well-sampled one:

**Repro:**
```
>>> kb.summarise_sample([42])
Sample(objects=2752512.0, stderr=inf, leaves_read=1, read_failures=0)
```
`stderr=inf` never appears in any `Verdict.msg` — I confirmed by reading
`verdict()`'s full source; it references `sample.objects`,
`sample.read_failures`, and nothing else from `Sample`.

This isn't a crash and it isn't wrong arithmetic — the field exists and is
correct — but its existence with no consumer is a real design gap: the module
computes exactly the number that would let it say "this estimate is not
trustworthy" and then throws it away before the one place that could act on
it.

---

## Finding 7 — negative `RAM_BUDGET_BYTES` is not rejected (SEV-4)

**File:** `tools/cache_catcher/key_budget.py`

```python
def ram_capacity_keys(ram_budget_bytes, bytes_per_key):
    if not ram_budget_bytes or not bytes_per_key or bytes_per_key <= 0:
        return None
    return int(ram_budget_bytes / bytes_per_key)
```

Only `bytes_per_key` is range-checked (`<= 0`); `ram_budget_bytes` is only
falsiness-checked, so a negative value (an operator typo like
`RAM_BUDGET_BYTES=-9663676416` in `/log/keybudget.env`) is truthy and passes
straight through, producing a negative "capacity". Because `effective_capacity`
picks the *minimum* of the known ceilings, a negative ram ceiling always wins
over a legitimate positive zone ceiling, however large.

**Repro:**
```
>>> kb.ram_capacity_keys(ram_budget_bytes=-9*1024**3, bytes_per_key=128.5)
-75203707
>>> kb.effective_capacity(zone_keys=80_000_000, ram_keys=-75203707)
Ceiling(keys=-75203707, name='ram')
>>> sample = kb.Sample(objects=35_800_000.0, stderr=100.0, leaves_read=256, read_failures=0)
>>> kb.verdict(sample, _, history=[])
Verdict(status='down', msg='OVER FLOOR: 35.8M objects, -48% of ram ceiling -75.2M, trend unknown')
```

Note the status is still `"down"` — this fails toward alerting, not toward
silence, so it's not the same class of danger as Finding 1. It's reported
because the message ("-48% of ram ceiling -75.2M") is nonsense that would
confuse whoever reads it, and because it shows the same "no range validation
on an env-sourced number" pattern as Finding 1's root cause.

---

## Held — tried to break it, could not

- **`summarise_sample`**: zero leaves → `Sample(None, None, 0, read_failures)`,
  correctly triggers `verdict`'s "sample failed" down-path. Single leaf →
  `stderr=inf` (see Finding 6 for why that's not surfaced, but the value
  itself is correct). Negative counts (`[-5, -10, -3]`) → propagates a
  negative `objects` honestly rather than crashing; not reachable via the real
  call path (`sample_cache` only ever passes `len(os.listdir(...))`, always
  ≥ 0). 65537 leaves → no crash, no meaningful slowdown (2ms), extrapolation
  correctly independent of `n` vs `total_leaves`.

- **`project_days_to`**: identical timestamps → `None` (via `elapsed <= 0`).
  Out-of-order history (`t1 < t0`) → `None` (same guard, since it produces
  negative `elapsed`). Single point → `None` (`len(history) < 2`). Target
  already passed → `0.0`. A genuinely shrinking series → `None` (via
  `per_day <= 0`). `+inf` in a data point → does not crash, degenerates to
  "already there" (`0.0` days), which is defensible. All independently
  reproduced.

- **`verdict`**: `floor=0`/`floor=-1` → always "OVER FLOOR" (degenerate but
  sane given the config). `horizon_days=0` → disables the "filling fast"
  early-warning branch entirely, which is a legitimate (if unobvious) reading
  of "give me zero days of runway before I care" — not a crash.
  `ceiling.keys=1` → "OVER FLOOR" immediately, formats as `0.0M` (rounding),
  no crash.

- **`kuma.push`**: `status=None` → does not raise (formatted as the string
  `"None"` by `urlencode`); the push just fails cleanly server-side, reported
  as `False`. `msg=bytes` → does not raise; `len()` and `quote_via` both
  accept bytes. An opener returning an object with no `.status` attribute →
  caught by the general `except Exception`, returns `False`, does not raise.

- **`load_cfg`**: a line with no `=` → silently skipped (falls to `DEFAULTS`).
  A duplicate key → last-value-wins (plain dict overwrite; not a crash or a
  surprising outcome). `=` embedded in the value (`SAMPLE_LEAVES=64=weird`) →
  captured verbatim as `"64=weird"` (correct `split("=", 1)` semantics; the
  eventual `int()` failure at the call site is caught by `probe_loop`'s
  `try/except`, degrading to a `"down"` push rather than crashing the thread).
  Blank lines and `#`-comment lines → skipped. A single 10 MB line → parses in
  ~4ms with no memory blowup.

- **`read_history` / `append_history`**: a line with no comma, a line with
  only one field, a non-numeric second field (`"abc"`), and a truncated final
  line with no trailing comma/newline are all correctly skipped via the
  existing `len(parts) >= 2` check and the `try/except ValueError`. The one
  exception is the `nan`/`inf` literal class — see Finding 1.

- **Migration 0018 ordering/atomicity**: read `src/orchestrator/db/migrate.py`
  directly rather than assuming. The production runner (`run_migrations` /
  `_run_migrations_locked`) wraps every unapplied migration's statements
  (INSERT then UPDATE, in that order) inside a single `BEGIN IMMEDIATE` … the
  loop … `COMMIT`, with `ROLLBACK` on any `sqlite3.Error`
  (`migrate.py:619-676`). So even if the UPDATE half failed for some reason,
  the INSERT would roll back with it — no partial-application window exists
  through the real runner. If the INSERT and UPDATE statements' order were
  swapped, the INSERT's `WHERE status = 'failed'` would find zero rows by the
  time it ran (the UPDATE having already flipped them to `'blocked'`), so the
  audit trail for all 19 rows would be silently lost while the status change
  itself still succeeded — order genuinely matters, and the file has it right
  (INSERT at lines 66-69, before UPDATE at lines 80-82).

- **Migration 0018 idempotency**: did not trust the test's docstring claim.
  Independently ran `tests/db/test_migration_0018_retire_failed_status.py`
  (`PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest
  tests/db/test_migration_0018_retire_failed_status.py -v`) — 8/8 passed,
  including `test_the_migration_is_idempotent`, which applies the raw SQL via
  `executescript()` twice against the same in-memory connection and asserts
  exactly one `measurement_transitions` row results. Traced *why* it's
  idempotent rather than just accepting the green test: both statements are
  predicated on `status = 'failed'`, which is self-excluding after the first
  run flips every matching row to `'blocked'` — idempotency here is a
  structural consequence of the predicate referencing the very column being
  mutated, not an explicit guard, and it holds.

- **Migration 0018 vs. an already-`blocked` row / an existing transitions row
  for the same game**: `test_an_already_blocked_row_is_not_disturbed` covers
  a row blocked for an unrelated reason — correctly untouched, and gets no
  spurious transition row, because the migration's predicate is `status =
  'failed'`, not `status IN ('failed','blocked')`. A row that already has
  *some* unrelated transition history is unaffected by anything in the
  migration — `measurement_transitions` is an append-only log by design, and
  the migration just appends one more row to it; multiple rows per `game_id`
  is normal and expected, not a conflict.

- **Migration 0018 vs. the circuit breaker**: read `jobs/measurement.py`
  directly. The breaker's rolling-window query is literally `WHERE downward =
  1 AND commanded = 0` (`measurement.py:261`), which textually matches the
  partial index `idx_measurement_transitions_window` created by migration
  0016 (`WHERE downward = 1 AND commanded = 0`). Migration 0018's INSERT sets
  `commanded = 1`, so its 19 rows are excluded from both the index and the
  breaker's count by construction — verified by reading the matching
  predicate on both sides, not assumed from the migration's own comments.

- **Migration 0018 touching a row it shouldn't**: `test_every_other_status_is_
  left_alone` covers `unknown`, `not_downloaded`, `up_to_date`,
  `pending_update`, `validation_failed` — none are touched, and the predicate
  is a plain `WHERE status = 'failed'` with no ownership filter (deliberately,
  per the file's own comment and `test_unowned_rows_are_migrated_too`), so
  there's no window for scope creep beyond what's already tested and
  documented.

## Note on scope of the single-writer guard section

I was asked to find a *new* evasion beyond issue #312's nine known ones, but
did not have #312's text in front of me (out of scope to fetch it), so I can't
positively confirm none of Finding 4's six forms overlap with an
already-catalogued evasion. What I can state with certainty, independently
reproduced above: all six are misses today, and three of them (schema
qualification, table alias, schema-qualified INSERT) are valid, executable
SQLite that genuinely writes `games.status`, not just theoretical regex gaps.
