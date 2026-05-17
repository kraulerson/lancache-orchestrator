# UAT-2 — Input-Validation Fuzz Report

**Agent:** Input-Validation Fuzz
**Scope:** BL3 (Settings) + BL4 (DB pool) input surfaces
**Files in scope:**
- `src/orchestrator/core/settings.py`
- `src/orchestrator/db/pool.py`
- `src/orchestrator/db/migrate.py`
**Methodology:** static enumeration + pathological-input proposals. No tests executed.

Severities use the project convention: **SEV-1** = data loss / boot crash, **SEV-2** = security or reliability defect, **SEV-3** = surprising-behavior defect, **SEV-4** = polish.

---

## 1. ORCH_* environment variables — per-field analysis

All fields share `env_prefix="ORCH_"`, `extra="ignore"`, `case_sensitive=False`, `env_file=".env"`, `secrets_dir="/run/secrets"`. pydantic-settings precedence: init kwargs > env > .env > /run/secrets > defaults.

### 1.1 `orchestrator_token: SecretStr` (alias `ORCH_TOKEN` or secret-file `orchestrator_token`)

**Declared validation:**
- Required (`...`).
- `_strip_token` (mode="before") strips whitespace from `str` or `SecretStr`. Other types fall through.
- `_check_token_length` (mode="after") rejects `< 32` chars on the `SecretStr` instance (so `ValidationError.input_value` is the redacted SecretStr — Bible §7.3).
- `__init__` re-wraps any token-related ValidationError as `ValueError("orchestrator_token validation failed: …")` to scrub.

**Gaps:**
- (G1) No upper bound. A 10 MB token loaded via `/run/secrets/orchestrator_token` is accepted and held in memory for the process lifetime. Memory-amplification surface; combined with `__init__`'s catch-all wrap, this never trips a length error.
- (G2) `_strip_token` only handles `str.strip()` — Python's default. This **does** strip embedded U+00A0 NBSP only on the boundary, not inside. Embedded control chars (NUL, CR, LF, VT) are preserved. A token like `"A" + "\x00" * 31 + "Z"` is 33 chars and accepted. No printable-ASCII assertion.
- (G3) Unicode-only tokens pass: 32 zero-width-joiners (U+200D × 32) is len()=32 in Python and accepted. Same-width as redacted display, but a token of all-whitespace inside (NBSP×32) is also accepted because `str.strip()` only strips ASCII whitespace + a few Unicode spaces but not all 25 code points of "whitespace".
- (G4) The defensive fallthrough in `_strip_token` (return `v`) means a non-`str`/non-`SecretStr` type — e.g. an `int` from a `.env` like `ORCH_TOKEN=0` — reaches the `mode="after"` validator as the original int. Pydantic auto-coerces to `SecretStr`, so this is recovered, but the contract isn't documented.
- (G5) `__init__`'s ValidationError catcher matches `"token" in str(loc).lower()` — any future field with "token" in its name (`refresh_token`, `csrf_token_secret`) inherits the scrub path silently. Probably desirable, worth documenting.
- (G6) `secrets_dir` is type-narrowed via `isinstance((str, Path))`, but `model_config["secrets_dir"]` is a literal `"/run/secrets"`. If a future contributor sets it to a list-of-paths (the type allows it), the shadow-warning is silently skipped — fail-open security warning. Suggest assertion at module load.

**Pathological inputs:**
- 10 MB token: `ORCH_TOKEN=$(python -c 'print("A"*10000000)')` — accepted, no diagnostic. Probes G1.
- Embedded NUL: `ORCH_TOKEN=$'A\0BCDEF…(>=32 chars total)'` — accepted, downstream comparators may truncate at NUL. Probes G2.
- All-NBSP: `ORCH_TOKEN=$'\xc2\xa0' * 40` — `str.strip()` keeps NBSP, accepted as 40-char string. Probes G3.
- All-whitespace ASCII: `ORCH_TOKEN="                                "` (32 spaces) — `_strip_token` reduces to "" (0), correctly rejected. Boundary works.
- 31 chars boundary: `ORCH_TOKEN=$(python -c 'print("A"*31)')` — must reject. (Verify test exists.)
- 32 chars boundary: `ORCH_TOKEN=$(python -c 'print("A"*32)')` — must accept. (Boundary on `<` not `<=`, looks correct.)
- CRLF mid-token: `ORCH_TOKEN=$'AAAA\r\nBBBB...AAAA' (>=32 incl CRLF)` — accepted; logging this token via a misformatted log line could inject log entries. Probes G2.
- Type confusion: `ORCH_TOKEN=true` from `.env` — pydantic likely coerces "true" to SecretStr("true"), len 4, rejected.

**Recommendations:**
- Add an explicit `len(v.get_secret_value()) <= 4096` cap (SEV-3).
- Reject embedded ASCII control chars (`< 0x20` except none) (SEV-2): one-liner test `assert ValueError when ORCH_TOKEN contains "\x00"`.
- Document the type-narrowed `secrets_dir` assumption and add a startup `assert isinstance(secrets_dir, (str, Path))`.

### 1.2 `api_host: str` (default `127.0.0.1`, min_length=1)

**Declared validation:** non-empty string. **No format validation.** Anything goes: `"not.a.host"`, `"::"`, `"0.0.0.0"`, `";rm -rf /"`, `"\x00"`.

**Gaps:**
- (G7) An attacker who can mutate env (or a typo) gets bound to whatever uvicorn accepts. The `_emit_config_warnings` warns only on _non-loopback_, not on **invalid**. Binding to `"127.0.0.1\nINJECTED"` would fail at uvicorn but only after startup logs may have leaked partial state.
- (G8) `_LOOPBACK_HOSTS` is exact-string-matched. `"127.0.0.001"` (octal-leading-zero, valid loopback alias on Linux) is **not** matched and triggers the non-loopback warning falsely (false-positive, SEV-4).

**Pathological:** `ORCH_API_HOST=""` (rejected by min_length), `ORCH_API_HOST=" 127.0.0.1 "` (accepted with leading space — uvicorn likely fails), `ORCH_API_HOST=$'127.0.0.1\nFoo'` (CRLF), `ORCH_API_HOST=255.255.255.255`, `ORCH_API_HOST=localhost.` (trailing dot).

**Recommendation:** validate via `ipaddress.ip_address(...)` OR known-hostname pattern. Treat unknown as warn. (SEV-3)

### 1.3 `api_port: int` (default 8765, ge=1, le=65535)

**Declared validation:** `1 ≤ port ≤ 65535`. **Boundaries correct.**

**Pathological:** `0` rejected; `65536` rejected; `-1` rejected; `"abc"` rejected; `"8765 "` (trailing space) — pydantic coerces, accepted; `"08765"` (octal-style) → 8765 in pydantic int coercion.

**Note:** binding to a privileged port (≤1024) on a non-root container would fail at uvicorn. No warning emitted. (SEV-4 polish.)

### 1.4 `cors_origins: list[str]`

**Declared validation:**
- Field default = `[]`.
- `_reject_empty_cors_origin` rejects any empty-string element.
- `_emit_config_warnings` warns if `"*"` is present.

**Gaps:**
- (G9) **No URL/origin format validation.** Accepted: `["javascript:alert(1)"]`, `["null"]`, `["http://"]`, `["http://evil.com\nSet-Cookie: x=y"]` (CRLF injection vector if echoed in CORS header). The wildcard warning is the only signal.
- (G10) pydantic-settings parses `ORCH_CORS_ORIGINS` from env as JSON when value starts with `[`. **Non-JSON string** like `ORCH_CORS_ORIGINS=http://a,http://b` is parsed as a 1-element list `["http://a,http://b"]` (pydantic v2 default behavior: only delimited via `env_parse_none_str` etc. unless `env_nested_delimiter` is set; not configured here). High operator-confusion risk. Worth a doc note. SEV-3.
- (G11) Deeply nested JSON (`[[[[...]]]]`) is rejected at type validation (`list[str]` requires str elements), so `[["a"]]` raises. Good. But `[null]` raises with a noisy ValidationError that might leak the rejected literal — token's scrub path doesn't apply here.
- (G12) Very long list (10 000 origins) — accepted, used by every preflight check linearly. No DoS at config layer but downstream CORS middleware may iterate.
- (G13) Element-level: a single 1 MB string is accepted; the Field has no per-element max_length.
- (G14) Unicode origins: `["http://ex​ample.com"]` (zero-width space) accepted, browser will not match — silent CORS misroute.

**Pathological:**
- `ORCH_CORS_ORIGINS='["", "https://x"]'` — rejected (good).
- `ORCH_CORS_ORIGINS='["javascript:alert(1)"]'` — accepted (G9).
- `ORCH_CORS_ORIGINS='https://a,https://b'` — accepted as 1-element (G10).
- `ORCH_CORS_ORIGINS='[null]'` — TypeError leaks "null" into ValidationError.
- `ORCH_CORS_ORIGINS='["http://x.com\r\nEvil: 1"]'` — accepted (G9 / CRLF).
- `ORCH_CORS_ORIGINS='["' + 'a'*10485760 + '"]'` — accepted, 10 MB string in memory.

**Recommendation:** add an after-validator that asserts each origin matches `^(https?://[^\s/]+|null|\*)$` and that no element exceeds 4096 chars. SEV-2 if API is internet-facing.

### 1.5 `log_level: Literal[...]`

**Declared validation:** strict enum of 5 strings.

**Gaps:** none significant. Lowercase variants ("info") rejected by Literal; case-sensitive=False applies to env-var name only, not value. Pydantic might coerce `"info"` if there's a custom validator — there isn't, so case-strict.

**Pathological:** `ORCH_LOG_LEVEL=info` rejected (case-strict on the value). May surprise. SEV-4.

### 1.6 `database_path: Path`, `steam_session_path: Path`, `epic_session_path: Path`, `lancache_nginx_cache_path: Path`

**Declared validation:** Path type. **No format validation.** No existence check, no permission check, no symlink check, no traversal check.

**Gaps:**
- (G15) Path traversal: `ORCH_DATABASE_PATH=/etc/passwd` — accepted, sqlite would attempt to open and likely overwrite. **No allowlist check.** SEV-2 if the orchestrator runs as root or with broad write perms.
- (G16) Relative paths (`../../foo`) accepted; `Path` doesn't normalize. Operator on macOS APFS gets a different effective path than under a container.
- (G17) Trailing-slash inconsistency: `ORCH_DATABASE_PATH=/var/lib/orchestrator/orchestrator.db/` — sqlite errors at open with "unable to open database file"; misleading message. SEV-4.
- (G18) Null-byte: `ORCH_DATABASE_PATH=/var/lib/orchestrator/x\x00.db` — Python's `Path` permits construction; sqlite3 raises ValueError("embedded null character") at connect time. Boot-time crash with a half-baked log. SEV-3.
- (G19) Case-mismatch on case-insensitive FS (HFS+/APFS default): two settings could refer to the same file (e.g. `/foo/db.sqlite` vs `/foo/DB.sqlite`); the migrate runner on macOS sees the same inode but the test layer thinks they're separate. SEV-4.
- (G20) `_assert_local_filesystem` calls `_detect_filesystem_type(db_path)` where `target = str(path if path.exists() else path.parent)`. If `path.parent` doesn't exist either (e.g. `/nonexistent/sub/db`), `stat -f` returns non-zero → "unknown" → boot warning (or fail-closed in strict). SEV-4 — the actual cause (parent doesn't exist) isn't surfaced.
- (G21) `lancache_nginx_cache_path` defaults to `/data/cache/cache/` (trailing slash). The slash matters for some downstream `os.path.join` calls. Worth normalizing in a validator.

**Pathological:**
- `ORCH_DATABASE_PATH=` (empty) — pydantic parses as `Path("")` → `Path('.')`. Accepted! sqlite tries to open `.` as a DB file and errors. SEV-3 — empty string should reject.
- `ORCH_DATABASE_PATH=/dev/null` — accepted; sqlite open succeeds, all writes vanish silently. SEV-2.
- `ORCH_DATABASE_PATH=/proc/self/mem` — accepted on Linux; sqlite errors but only after init.
- `ORCH_DATABASE_PATH=$'/tmp/db\x00.evil'` — NUL byte path.
- `ORCH_DATABASE_PATH=/tmp/$(touch /tmp/owned)` — literal string, no shell expansion at pydantic layer. Safe.
- Symlink: `ORCH_DATABASE_PATH=/tmp/link → /etc/shadow`. Accepted; opens follow symlink. Document that sessions/db paths must not be writable by other users.

**Recommendation:** add a path-validator that rejects empty string, NUL bytes, and (optionally) requires absolute paths in production. SEV-2.

### 1.7 `require_local_fs: Literal["strict","warn","off"]`

**Declared validation:** strict enum. Good. Lowercase strings only — uppercase is rejected. SEV-4 doc.

### 1.8 `cache_slice_size_bytes: int` (gt=0)

**Declared validation:** strictly positive. Upper bound: **none**. A value of 2^63 is accepted; downstream slice-iteration would loop effectively forever or OOM. SEV-3.

### 1.9 `cache_levels: str` (pattern=`^\d+(:\d+)*$`)

**Declared validation:** dot-style regex.

**Gaps:**
- (G22) **No catastrophic backtracking risk** — the regex is purely linear (`\d+` followed by an optional `(:\d+)*` non-overlapping group). Confirmed via inspection: no nested quantifiers, no alternation overlap. **No ReDoS.** (BL3 audit SEV-4 should be downgraded if it referenced ReDoS — verify.)
- (G23) No semantic check: `"99999999:99999999"` matches the regex. Lancache's nginx slice config supports values 0–2 typically. Accepted but downstream nginx errors at runtime. SEV-4.
- (G24) Empty string fails regex (good). `":2"` fails (good). `"2:"` fails (good — the regex is anchored both ends).
- (G25) Unicode digits via `\d` — Python's `\d` by default matches `[0-9]` plus Unicode digits. So `"٢:٢"` (Arabic-Indic 2) **matches** but is semantically meaningless. SEV-3.

**Pathological:** `ORCH_CACHE_LEVELS="٢:٢"` — accepted; lancache fails. `ORCH_CACHE_LEVELS=$(python -c 'print("1:"*100000+"1")')` — a 200 KB string still matches in linear time, no hang.

**Recommendation:** tighten pattern to `^[0-9]+(:[0-9]+)*$` (ASCII-only) with element-count cap. SEV-3.

### 1.10 `chunk_concurrency: int` (ge=1, le=256)

**Declared validation:** `1 ≤ x ≤ 256`. Boundaries correct. Warning emitted if `> 32` (Spike-F validated bound). Good.

### 1.11 `manifest_size_cap_bytes: int` (gt=0)

**Declared validation:** strictly positive. **No upper bound.** `2^63` accepted. Downstream allocator behavior depends on caller. SEV-3.

### 1.12 `epic_refresh_buffer_sec: int` (ge=0)

**Declared validation:** non-negative. **No upper bound** — `2^63 - 1` accepted; would render Epic refresh effectively never. SEV-4.

### 1.13 `steam_upstream_silent_days: int` (ge=1)

**Declared validation:** ≥ 1. **No upper bound.** SEV-4.

### 1.14 BL4 fields — `pool_readers`, `pool_busy_timeout_ms`, `db_cache_size_kib`, `db_mmap_size_bytes`, `db_journal_size_limit_bytes`

| Field | ge | le | Notes |
|---|---|---|---|
| `pool_readers` | 1 | 32 | Bounds correct. Warning emitted if `pool_readers > chunk_concurrency` (good). |
| `pool_busy_timeout_ms` | 0 | 60_000 | `0` means **no busy-wait at all** — every contended write becomes immediate `database is locked` error. Documented? The `ge=0` is intentional but operationally dangerous default if mis-set. SEV-3 — consider `ge=100` with explicit override, or warn on `0`. |
| `db_cache_size_kib` | 1024 | 1_048_576 | Bounds correct (1 MiB to 1 GiB cache). |
| `db_mmap_size_bytes` | 0 | 17_179_869_184 | `0` disables mmap (valid SQLite idiom). 16 GiB cap. Bounds reasonable. |
| `db_journal_size_limit_bytes` | 1_048_576 | 1_073_741_824 | 1 MiB to 1 GiB. Bounds OK. |

**Boundary off-by-ones:** none found. All `ge`/`le` are inclusive (pydantic semantics).

**Subtle gap (G26):** `pool_readers <= chunk_concurrency` warning fires only when over-provisioned. There's no _under-provisioned_ warning when `chunk_concurrency >> pool_readers` — readers will be a contention bottleneck. SEV-4.

**Subtle gap (G27):** `db_cache_size_kib` is converted to a negative pragma value (`-self._cache_size_kib`) in `_open_connection`. Boundary: max 1_048_576 KiB → pragma `-1048576` → SQLite interprets as "use 1 GiB". A future contributor raising the `le` to e.g. `2_147_483_648` would overflow SQLite's signed-32-bit pragma argument. **Not currently exploitable.** Documenting the inferred bound prevents future regressions. SEV-4.

---

## 2. `Pool.create(...)` kwargs and SQL params

### 2.1 `Pool.create()` keyword args

**Declared validation:** none at the call site. The `_PoolCreator` accepts whatever the caller passes; `_async_create` calls `cls(...)` which assigns to private attrs without checking ranges.

**Gaps:**
- (G28) A direct caller can pass `readers_count=0` — `asyncio.Queue(maxsize=0)` is **unbounded** in Python (sentinel value). The `for idx in range(0)` loop won't open any readers; subsequent `_checkout_reader` will block forever on `await self._readers.get()`. **Hangs the calling coroutine.** Caught for the singleton path because Settings clamps `pool_readers ≥ 1`, but not enforced by the Pool class itself. **SEV-2 — silent hang.** Recommend an `assert readers_count >= 1` in `__init__` or `_async_create`.
- (G29) `readers_count=-1` — `asyncio.Queue(maxsize=-1)` is also unbounded; `range(-1)` yields nothing → same hang.
- (G30) `database_path` accepts `str | Path`. No NUL-byte check; passes to `aiosqlite.connect` which raises ValueError. The `try/except BaseException → _teardown_connections` catches it, but the catch is `BaseException` which would also swallow `KeyboardInterrupt`. **Style/SEV-4.**
- (G31) `busy_timeout_ms=-1` not blocked at the Pool layer (Settings is). PRAGMA busy_timeout silently accepts negative as 0. SEV-4.
- (G32) URI form: `database_path="file:foo?mode=memory&cache=shared"` triggers `uri=True`. Operators discovering this can craft URIs with `vfs=` and other options. Not a vulnerability (caller has trust) but undocumented. SEV-4 doc.

**Pathological:**
```python
await Pool.create(database_path=":memory:", readers_count=0, busy_timeout_ms=5000, ...)
# Hangs on first read_one() forever. Probes G28.
```

```python
await Pool.create(database_path="file:test?mode=memory&cache=shared",
                  readers_count=4, ..., skip_schema_verify=True)
# Should work; verify URI handling parses correctly.
```

### 2.2 SQL params via `read_one`/`execute_write`/etc.

`Sequence[Any] | Mapping[str, Any]` — passes verbatim to aiosqlite/sqlite3.

**Declared validation:** none — sqlite3 enforces type bindability (`int`, `float`, `str`, `bytes`, `None`, or `__conform__` impls).

**Gaps:**
- (G33) Embedded NUL in `str` param: SQLite **stores** NUL fine in a TEXT column (since 3.0). But parsers downstream that read the value with `c_string` semantics truncate. Caller-side risk only. SEV-4 doc.
- (G34) Very large `bytes`: a 2 GiB BLOB param exceeds SQLITE_MAX_LENGTH (default 1 GiB). aiosqlite raises `OperationalError("string or blob too big")`. The `_wrap_aiosqlite_error` catch-all converts to `QueryError` — **not** classified as a syntax error or integrity violation. **The "params" log field is `_shape(params)` — type names only, not values.** Good — values aren't reflected. SEV-4: consider adding a "too big" branch for clarity.
- (G35) Non-bindable types: passing a `set` or arbitrary object → sqlite3.InterfaceError("Error binding parameter"). Caught in `aiosqlite.Error` → wrapped in QueryError. Loses the "this is a bug, not a runtime issue" signal. Consider classifying as `QuerySyntaxError` (programmer-bug class). SEV-3.
- (G36) `_shape` calls `type(v).__name__` on each value. If `v` is an object whose `type().__name__` raises (extremely rare — only if someone maliciously overrides `__class__.__name__` via metaclass tricks), the log call itself raises. **Not realistically exploitable.** SEV-4.
- (G37) `_template_only` strips numeric / string / hex BLOB literals from logged SQL before logging, but does **not** strip identifier names. SQL with embedded operator-supplied identifiers (e.g. dynamic `f"SELECT * FROM {table}"` upstream) leaks the table name. Caller responsibility. SEV-4 doc.
- (G38) `params="<many>"` for `execute_many_write` / `execute_many` — a one-time string literal, not a placeholder. Hard-coded value confused for a placeholder by log consumers. SEV-4 polish.

**Pathological:**
- `await pool.execute_write("INSERT INTO foo VALUES (?)", (None,))` — works. `(...,)` with `set()` raises InterfaceError → wrapped opaquely.
- `await pool.execute_write("INSERT INTO foo VALUES (?)", (b"x" * (2**31),))` — raises "string or blob too big". Verify wrapping.
- Streaming: `async for row in pool.read_stream("SELECT … LIMIT 999999999", ()):` — Python iterates, fine.
- `await pool.read_one("SELECT 1 -- " + ";"*1_000_000)` — long SQL is logged via `_template_only` which is regex-driven and runs in linear time on this input. No ReDoS.

### 2.3 `_row_to_dataclass(cls, row)` — dataclass mapping

Located at `pool.py:263`.

**Gap (G39, SEV-2):** the function calls `fields(cls)` without verifying `cls` is a dataclass. Passing a non-dataclass class raises `TypeError("must be called with a dataclass type or instance")` from `dataclasses.fields`. The error is **not** caught by `aiosqlite.Error` handlers in `read_one_as`/`read_all_as`, so it propagates as a raw `TypeError` to the caller — **not** wrapped in `QueryError`. Inconsistent error contract.

**Gap (G40, SEV-3):** `kwargs = {k: row[k] for k in keys if k in field_names}`. Row columns **not** in field_names are silently dropped. If the dataclass has extra **required** fields (no default, not in row), `cls(**kwargs)` raises `TypeError: __init__() missing 1 required positional argument`. Caller-confusing — recommend explicit "row missing required field X for dataclass Y" error.

**Gap (G41, SEV-4):** the dataclass's `__init__` may run validation that raises (e.g. `__post_init__`). That exception is also not wrapped. Inconsistent error contract.

**Gap (G42, SEV-3):** field-name collision with a SQL reserved/shadow name — e.g. dataclass with field `id` and row with column `id` (case sensitive in Python, case-insensitive in SQL). Row keys come from `aiosqlite.Row.keys()` which returns the SQL column names as declared. If the dataclass field is `Id` and the row is `id`, the field is silently dropped (G40). Document column-name → field-name expectation.

**Pathological one-liner:**
```python
class NotADataclass: pass
await pool.read_one_as(NotADataclass, "SELECT 1 AS x")
# Raises raw TypeError, not QueryError. Probes G39.
```

```python
@dataclass
class HasRequired: a: int; required_b: int  # no default
await pool.read_one_as(HasRequired, "SELECT 1 AS a")
# Raises TypeError missing 'required_b'. Probes G40.
```

---

## 3. `migrate.run_migrations(db_path, migrations_dir=...)`

### 3.1 `db_path: str | os.PathLike[str]`

**Declared validation:** coerced to `Path(db_path)` first thing.

**Gaps:** all of G15–G21 from §1.6 apply (path traversal, NUL byte, dev-null, symlink). Plus:
- (G43) `Path("")` → `Path('.')` → `_assert_local_filesystem(Path('.'))` checks the cwd's filesystem. On a network-mounted cwd, surprising "refuses to boot" behavior. SEV-3.
- (G44) `Path` is not normalized (`.resolve()` not called), so symlinks aren't followed before the FS-type detection. A `/var/lib/orchestrator/db` symlink to `/mnt/nfs/db` might fool detection: `_detect_filesystem_type` calls `stat -f` on the link's path, which on macOS returns the **target's** filesystem. On Linux, `/proc/self/mountinfo` walks up parent paths — if the link target is on NFS but the link itself is on apfs, the longest-prefix-match logic in `_detect_filesystem_type` returns the link's mount, **not** the target's. **Network FS detection bypass via symlink.** SEV-2.

**Pathological:**
```sh
ln -s /mnt/nfs/foo.db /var/lib/orchestrator/orchestrator.db
ORCH_DATABASE_PATH=/var/lib/orchestrator/orchestrator.db python -m orchestrator.db.migrate ...
# Linux: detection sees apfs/ext4, accepts. SQLite then writes WAL to NFS-backed file → silent corruption.
```

### 3.2 `migrations_dir: Path | None`

**Declared validation:** None (defaults to packaged resources).

**Gaps:**
- (G45) Operator passing a hostile `migrations_dir` gets to control SQL applied to the production DB. **Trust boundary — caller is trusted.** Document. SEV-4.
- (G46) `_iter_migration_files` calls `entry.read_text(encoding="utf-8")` — files with non-UTF-8 bytes raise `UnicodeDecodeError`, **not** `MigrationError`. Inconsistent. Wrap in try/except. SEV-3.
- (G47) `_MIGRATION_NAME_RE = ^(\d{4})_([a-z0-9_]+)\.sql$` rejects uppercase letters in names — silent skip via the `if not m: continue`. An operator naming `0001_AddTable.sql` finds the migration **silently ignored**. Then post-apply sanity check fails because expected tables aren't created. SEV-3 — should warn-or-error on regex-mismatched files.
- (G48) Duplicate IDs caught (good). But `_load_migrations` checks **after** sort, so IDs are sorted ascending; the duplicate-check passes if both files have the same `mid`. Verify: `_load_migrations` returns sorted, then a `seen` set check raises on dup. Looks correct.
- (G49) The CHECKSUMS line parser splits on whitespace. A filename containing whitespace (which the migration regex won't match anyway, so it'd never be in `migrations`) but appearing in CHECKSUMS would split into >3 parts and raise. Defensive — good.

### 3.3 CHECKSUMS manifest

(G50, SEV-4) Empty file → `result = {}` → all `_verify_checksum_manifest` checks pass with no migrations. If migrations exist on disk but CHECKSUMS is empty, `missing = {1,2,3,…}` raises `migration files not in CHECKSUMS manifest`. Good.

(G51, SEV-3) Comments-only CHECKSUMS (every line begins with `#`) → `result = {}` → same as empty. Same treatment.

---

## 4. Surfaces with NO validation

The following fields/inputs have **no semantic validation beyond pydantic types**, only bounds:

1. `api_host` — accepts arbitrary string (no IP/hostname check).
2. `cors_origins` — accepts any non-empty string per element (no URL format check).
3. `database_path` / `steam_session_path` / `epic_session_path` / `lancache_nginx_cache_path` — accept any path including symlinks, traversal, dev nodes, empty string.
4. SQL `params` (Pool) — type-bindable check only (sqlite3-enforced).
5. Dataclass `cls` arg in `read_one_as` / `read_all_as` — no `is_dataclass()` check.
6. `migrations_dir` arg to `run_migrations` — trusted-caller only, no validation.
7. CHECKSUMS comment-only file — silently treated as empty manifest.

---

## 5. Inputs that crash, hang, or behave surprisingly

| # | Input | Behavior | Severity |
|---|---|---|---|
| 1 | `Pool.create(readers_count=0)` (direct, bypassing settings) | First `read_one()` hangs forever — `asyncio.Queue(maxsize=0)` is unbounded, but pool opened 0 readers; `await self._readers.get()` blocks indefinitely. | **SEV-2** |
| 2 | `Pool.create(readers_count=-1)` | Same hang as #1. | **SEV-2** |
| 3 | Symlink for `database_path` pointing to NFS | `_detect_filesystem_type` likely returns the link's mount, not the target's; WAL written to NFS, silent corruption. | **SEV-2** |
| 4 | `ORCH_DATABASE_PATH=/dev/null` | sqlite open succeeds, all writes vanish silently. No diagnostic. | **SEV-2** |
| 5 | `ORCH_DATABASE_PATH=` (empty string) | Coerces to `Path('.')`, sqlite opens cwd as DB → cryptic error. | **SEV-3** |
| 6 | `read_one_as(NotADataclass, "...")` | Raises raw `TypeError` from `dataclasses.fields`, not wrapped in `QueryError`. | **SEV-2 (contract)** |
| 7 | `pool_busy_timeout_ms=0` (within bounds) | Every contended write fails immediately with `database is locked`. | **SEV-3** |
| 8 | `ORCH_CORS_ORIGINS=https://a,https://b` (no JSON brackets) | Parsed as 1-element list `["https://a,https://b"]` — **operator surprise**, every browser fails CORS. | **SEV-3** |
| 9 | `0001_AddTable.sql` (uppercase in name) | Silently ignored by regex; post-apply sanity then fails because expected tables missing — **misleading error message**. | **SEV-3** |
| 10 | Migration file with non-UTF-8 bytes | Raises `UnicodeDecodeError`, not `MigrationError`. | **SEV-3** |
| 11 | `cache_levels="٢:٢"` (Arabic-Indic digits) | Regex `\d` matches Unicode digits → accepted, lancache/nginx fails downstream. | **SEV-3** |
| 12 | `ORCH_TOKEN` of 10 MB | Accepted, held in memory; no upper bound. | **SEV-3** |
| 13 | `ORCH_TOKEN` containing `\x00` or `\r\n` | Accepted (only ASCII whitespace stripped, no control-char rejection). Log-injection vector if echoed. | **SEV-2** |
| 14 | `ORCH_DATABASE_PATH=/tmp/x\x00.db` | Path constructs OK; sqlite raises `ValueError` at connect. Cryptic error. | **SEV-3** |
| 15 | `cache_slice_size_bytes=2**63` | Accepted, downstream slicer loops/OOMs. | **SEV-3** |

---

## 6. Boundary off-by-one audit

Reviewed every `ge`/`le`/`gt`/`lt`/`min_length`/`max_length`/length-check:

| Field | Boundary | Inclusive? | Off-by-one? |
|---|---|---|---|
| `orchestrator_token` | `len() < 32` rejects | exclusive on lower | **Correct.** 32 accepted, 31 rejected. |
| `api_port` | `ge=1, le=65535` | both inclusive | **Correct.** |
| `cache_slice_size_bytes` | `gt=0` | exclusive 0 | **Correct.** 1 accepted. |
| `chunk_concurrency` | `ge=1, le=256` | both inclusive | **Correct.** |
| `manifest_size_cap_bytes` | `gt=0` | exclusive 0 | **Correct.** |
| `epic_refresh_buffer_sec` | `ge=0` | inclusive 0 | **Correct** (0 = no buffer, possibly intended). |
| `steam_upstream_silent_days` | `ge=1` | inclusive 1 | **Correct.** |
| `pool_readers` | `ge=1, le=32` | both inclusive | **Correct.** |
| `pool_busy_timeout_ms` | `ge=0, le=60000` | both inclusive | `0` is operationally hazardous (see §1.14). Spec note. |
| `db_cache_size_kib` | `ge=1024, le=1048576` | both inclusive | **Correct.** Sign flip happens at PRAGMA; range fits int32. |
| `db_mmap_size_bytes` | `ge=0, le=17_179_869_184` | both inclusive | **Correct.** 16 GiB cap. |
| `db_journal_size_limit_bytes` | `ge=1_048_576, le=1_073_741_824` | both inclusive | **Correct.** |
| `cache_levels` regex | anchored both ends | n/a | **Correct.** |
| `cors_origins` | empty-element rejected | n/a | **Correct.** No max-element-count or per-element max-length. |
| `_replacement_storm` | `> 3 in 60s` | exclusive | **Correct** — 4th replacement triggers storm guard. |
| `_safe_close` timeout | `2.0s` | n/a | **Correct.** |
| `close_pool` timeout | `30.0s` | n/a | **Correct.** |
| `health_check` probe timeout | `1.0s` per probe | n/a | **Correct.** |

**No off-by-one defects found** in the typed bounds. All issues are missing-bound, not wrong-bound.

---

## 7. Recommended one-liner regression tests (drafts)

```python
# G28: readers_count=0 should fail loudly, not hang
async def test_pool_create_zero_readers_raises():
    with pytest.raises((ValueError, AssertionError)):
        await Pool.create(database_path=":memory:", readers_count=0,
                          busy_timeout_ms=5000, cache_size_kib=2048,
                          mmap_size_bytes=0, journal_size_limit_bytes=1_048_576,
                          skip_schema_verify=True)

# G39: read_one_as with non-dataclass should wrap as QueryError or TypeError(documented)
async def test_read_one_as_non_dataclass(tmp_pool):
    class NotDC: pass
    with pytest.raises((TypeError, QueryError)):
        await tmp_pool.read_one_as(NotDC, "SELECT 1 AS x")

# G15/G44: symlink to network FS bypasses _assert_local_filesystem (Linux/macOS variant)
def test_symlink_to_nfs_detected(monkeypatch, tmp_path):
    target = tmp_path / "nfs_mount" / "db"
    target.parent.mkdir()
    link = tmp_path / "db.sqlite"
    link.symlink_to(target)
    monkeypatch.setattr(migrate, "_detect_filesystem_type", lambda p: "nfs" if "nfs_mount" in str(p) else "apfs")
    with pytest.raises(MigrationError, match="nfs"):
        migrate.run_migrations(link)  # currently passes — should FAIL

# G47: uppercase migration filename is silently ignored — should warn or error
def test_migration_filename_case_mismatch(tmp_path):
    (tmp_path / "0001_AddTable.sql").write_text("CREATE TABLE x (id INT);")
    (tmp_path / "CHECKSUMS").write_text("0001 <sha> 0001_AddTable.sql\n")
    with pytest.raises(MigrationError, match="not matched"):
        migrate.run_migrations(tmp_path / "db.sqlite", migrations_dir=tmp_path)

# G13: per-origin length cap
def test_cors_origin_length_cap(monkeypatch):
    monkeypatch.setenv("ORCH_TOKEN", "a"*32)
    monkeypatch.setenv("ORCH_CORS_ORIGINS", '["https://' + 'a'*10000 + '.com"]')
    with pytest.raises(ValidationError):
        Settings()

# G2/G13: token with embedded NUL should reject
def test_token_rejects_control_chars(monkeypatch):
    monkeypatch.setenv("ORCH_TOKEN", "A" + "\x00"*31 + "Z")
    with pytest.raises(ValueError):
        Settings()
```

---

## 8. Summary of issue counts

- **SEV-2 (security/reliability):** 6 — symlink FS-bypass, `readers_count=0` hang, `/dev/null` accepted, NUL/CRLF in token, dataclass `cls` no-validate, path traversal allowlist absent.
- **SEV-3 (surprising-behavior):** 11 — token byte cap, CORS format/CSV-confusion/length, path empty-string, `pool_busy_timeout_ms=0`, cache-levels Unicode digits, migration name case-sensitivity, non-UTF-8 migration files, dataclass missing field message, params non-bindable wrap, NUL byte in path, `cache_slice_size_bytes` upper bound.
- **SEV-4 (polish):** 14 — loopback canonicalization, doc gaps, log-template polish, `_safe_close` exception class width, etc.

**No SEV-1 found.** No input observed that causes data loss or boot crash through validation alone (all "crash" inputs result in cryptic-but-recoverable errors).

**No off-by-one boundary defects** in the existing typed bounds; all defects are **missing** bounds rather than **wrong** bounds.
