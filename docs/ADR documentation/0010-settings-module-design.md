# ADR-0010: Settings Module Design — pydantic-settings BaseSettings, `ORCH_` Prefix, Redaction-Safe Primitives

**Status:** Accepted
**Date:** 2026-04-23
**Phase:** 2 (Construction), Milestone B, Build Loop 3 (ID4)
**Related:** ADR-0001 (Orchestrator Architecture), ADR-0008 (Migration Runner), ADR-0009 (Logging Framework)
**Feature:** ID4-settings

<!-- Last Updated: 2026-04-23 -->

## Context

Every feature in Milestone B+ (DB pool, FastAPI app, Steam/Epic adapters,
validator, scheduler, CLI) reads configuration through a single typed module.
This module is load-bearing: a field added here is referenced by every
downstream consumer; a regression here is felt everywhere.

The Project Bible §2 pre-commits the stack to `pydantic` v2 + `pydantic-settings`.
Bible §7.3 commits the bearer token to a Docker secret at
`/run/secrets/orchestrator_token`, minimum 32 characters, container refuses
to start if missing. Bible §8 commits to structured logging with secret
redaction (TM-012).

The live questions for BL3 were: shape (flat vs. nested), scope (which fields),
source precedence, lifecycle (singleton pattern), secrets-file directory,
redaction strategy, validation scope, env-var prefix, and test isolation.

A 13-question brainstorm walked through the decision space with A/B/C options.
The spec (`docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`)
records the full decision trail.

This ADR records the load-bearing architectural decisions behind the final
implementation, plus the two SEV-2 findings surfaced by the Phase 2.4 security
re-audit and the fixes that closed them.

## Decisions

### D1 — Flat 16-field `BaseSettings` (no nested sub-models)

**Context:** With 16 fields spanning 5 logical domains (core/API, database,
platform sessions, Lancache topology, miscellaneous), nested sub-models were
attractive for readability but imposed two concrete costs: `env_nested_delimiter`
requires operators to type `ORCH_PLATFORM__STEAM__SESSION_PATH` instead of
`ORCH_STEAM_SESSION_PATH`, and the Docker-secret file naming convention
(file names map directly to field names) doesn't support nested paths.

**Decision:** Single flat `Settings` class. Each field is top-level. Env vars
map as `ORCH_<FIELD_NAME_UPPER>`. The one field-name collision
(`orchestrator_token` → `ORCH_ORCHESTRATOR_TOKEN`) is handled via
`validation_alias=AliasChoices("ORCH_TOKEN", "orchestrator_token")` so the
env name is `ORCH_TOKEN` while the secrets-file name stays `orchestrator_token`
(Bible §7.3 verbatim).

**Consequence:** Adding a field is a one-line change. Scaling concern
(30+ fields becoming unwieldy) is deferred until real pressure arrives; the
refactor path to a hybrid (sub-models for 3+-field clusters) is mechanical.

### D2 — Default pydantic-settings source order + shadow warning

**Context:** pydantic-settings' default order is
`init kwargs > env vars > .env > secrets_dir files > defaults`. The
deployment model (Docker-secret-mounted `/run/secrets/orchestrator_token`,
env for everything else) raised a concern: an accidental `ORCH_TOKEN`
in compose would silently shadow the Docker secret without any visibility.

**Decision:** Keep the default order (don't invert expectations for future
maintainers). Add a post-init `@model_validator(mode="after")` that emits
`log.warning("config.secret_shadowed_by_env", ...)` when env `ORCH_TOKEN`
AND the file `/run/secrets/orchestrator_token` both exist. The warning
changes nothing behaviorally — it only surfaces the shadow so the operator
can diagnose.

**Consequence:** Three additional diagnostic warnings ride the same validator:
`config.api_bound_non_loopback` (when `api_host` isn't `{127.0.0.1, ::1, localhost}`),
`config.cors_wildcard` (when `"*"` is in `cors_origins`), and
`config.chunk_concurrency_unvalidated` (when `chunk_concurrency > 32`, the
Spike F ceiling). All four are fire-and-forget at construction — zero
performance cost beyond boot.

### D3 — `@lru_cache` singleton via `get_settings()`

**Context:** Consumers need a shared `Settings` instance but the module
shouldn't instantiate at import time (Bible §7.3's "container refuses to
start on missing token" should fire at `main()`, not on `import` — otherwise
`orchestrator-cli --help` breaks in any container whose secret isn't mounted).

**Decision:** Wrap `Settings()` in `@lru_cache` as `get_settings()`. First
call constructs (and triggers validation + warnings). Subsequent calls return
the cached instance. A `reload_settings()` helper clears the cache for tests
and future SIGHUP-style reloads.

**Consequence:** Tests must call `get_settings.cache_clear()` between
scenarios; this is baked into the autouse `_isolated_env` fixture in
`tests/core/conftest.py`. Direct `Settings(...)` construction is allowed
(and used by tests) but forfeits the singleton guarantee.

### D4 — Secret handling — `SecretStr` + three defense layers against leaks

**Context:** The Phase 2.4 re-audit sub-agents found two SEV-2 token-leak
primitives that `SecretStr` alone doesn't cover.

**Decision:** Three complementary layers:

1. **`SecretStr` for in-memory handling.** `orchestrator_token: SecretStr`.
   `_strip_token` (mode=`before`) re-wraps after stripping. Pydantic's
   default redaction holds for `repr`, `str`, `model_dump`,
   `model_dump(mode="json")`, `model_json_schema` — verified by a 5-shape ×
   3-serialization parametrized regression suite (15 assertions).
2. **`__reduce__` override to block pickling.** `SecretStr._secret_value`
   serializes cleartext through Python's default `__reduce__`. `Settings`
   overrides `__reduce__` to raise `TypeError("Settings is not pickle-safe …")`.
   Prevents a future DX-sugar consumer (multiprocessing task args, on-disk
   cache, Celery) from accidentally writing the raw token.
3. **`__init__` wrapper to scrub `ValidationError.input_value`.** pydantic
   core tracks the raw input into `ValidationError.input` unconditionally.
   A rotation-failure startup (operator writes a 31-char token) would log
   the candidate token via `ValidationError.__str__`. `Settings.__init__`
   catches `ValidationError`, filters errors whose `loc` contains `"token"`,
   and re-raises as `ValueError` with a scrubbed message. Non-token errors
   propagate unchanged as `ValidationError`.

**Consequence:** Three regression tests (pickle blocked, pickle raises with
the expected message, short-token ValueError doesn't echo raw). Three
existing tests updated from `ValidationError` to `ValueError` expectations
on token-related failures. Consumers catching `ValidationError` at startup
must also catch `ValueError` (or `Exception`) to handle token-field
failures; this is documented in the module docstring and in the eventual
BL5 entry-point handler.

### D5 — Field-shape validation only; no filesystem checks

**Context:** Temptation: validate that `database_path.parent.exists()` and
`os.access(..., W_OK)` at `Settings()` construction. This surfaces deployment
errors early.

**Decision:** No filesystem checks in `Settings`. Every validator is
field-shape only (min_length, range, regex, enum). Filesystem responsibility
lies with consumers (`db.migrate.run_migrations()` verifies the DB path;
Steam/Epic session readers verify their own paths; validator verifies the
Lancache cache path).

**Consequence:** `Settings` is trivially testable — no tmpdir staging
required for most tests. Downstream consumers catch their own path errors
with domain-specific diagnostics. Test time stays sub-second for the 67
settings tests.

## Edge-case resolutions

**E1 — `secrets_dir` type narrowing.** `SettingsConfigDict["secrets_dir"]`
is typed `Path | Sequence[Path|str] | None`. The shadow-warning check
does `Path(secrets_dir) / "orchestrator_token"`, which mypy rejects on
the union type. Resolution: narrow via `isinstance(secrets_dir, (str, Path))`
before the concatenation. The project uses a single str, so the narrowing
path covers 100% of runtime; the `None` and `Sequence` branches silently
skip the warning (correct — nothing to shadow against).

**E2 — Defensive `_strip_token` fallthrough.** `@field_validator(mode="before")`
receives the raw input (str, SecretStr, or — theoretically — some other type
passed via init kwarg). The fallthrough `return v` handles the "other type"
case; pydantic's coercion layer rejects it. Marked `# pragma: no cover`
because the branch isn't reachable from any realistic caller.

**E3 — Test fixture matches ID3's structlog reset pattern.** `capsys` (not
`caplog`) captures structlog's JSON output. The autouse `_isolated_env`
fixture calls `structlog.reset_defaults()` + `structlog.contextvars.clear_contextvars()`
before and after each test, matching the pattern in `tests/core/test_logging.py`.

## References

- Spec: `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`
- Plan: `docs/superpowers/plans/2026-04-23-id4-settings-module.md`
- Security audit: `docs/security-audits/id4-settings-security-audit.md`
- Bible §2 (tech stack), §7.3 (API auth), §8 (observability), §10.3 (SAST rules)
- pydantic-settings v2 docs (`secrets_dir` behavior, `AliasChoices`,
  `settings_customise_sources` — verified via Context7)

---

## Addendum (2026-04-25, BL4): Settings expansion for DB pool

BL4 (DB pool) added 5 new typed fields to `Settings` for pool sizing and
SQLite PRAGMA tunables. Same `ORCH_*` prefix convention; same `Field()`
constraint pattern; one new `@model_validator(mode="after")` warning.

| Field | Type | Default | Bounds | Env var | Purpose |
|---|---|---|---|---|---|
| `pool_readers` | `int` | `8` | 1..32 | `ORCH_POOL_READERS` | Reader-pool size |
| `pool_busy_timeout_ms` | `int` | `5000` | 0..60_000 | `ORCH_POOL_BUSY_TIMEOUT_MS` | SQLite `busy_timeout` PRAGMA |
| `db_cache_size_kib` | `int` | `16384` | 1024..1_048_576 | `ORCH_DB_CACHE_SIZE_KIB` | Per-connection page cache (KiB) |
| `db_mmap_size_bytes` | `int` | `268_435_456` | 0..17_179_869_184 | `ORCH_DB_MMAP_SIZE_BYTES` | mmap window (bytes) |
| `db_journal_size_limit_bytes` | `int` | `67_108_864` | 1_048_576..1_073_741_824 | `ORCH_DB_JOURNAL_SIZE_LIMIT_BYTES` | WAL truncate threshold |

**New diagnostic warning:** `config.pool_readers_over_provisioned` fires
when `pool_readers > chunk_concurrency` — readers will idle since the
chunk-fanout consumer can't saturate them. Same fire-and-forget
construction-time pattern as the existing 4 warnings (D2 above).

**Memory baseline** (documented in FEATURES.md Feature 4 + README):
`(pool_readers + 1) × db_cache_size_kib + db_mmap_size_bytes`. Default
config = `9 × 16 MiB + 256 MiB ≈ 400 MiB` resident. Operators on
constrained hardware (DXP4800 NAS has 4 GB RAM total) should tune
`pool_readers` and `db_cache_size_kib` together — halving readers and
cache yields a `5 × 8 MiB + 256 MiB ≈ 296 MiB` profile.

**Cross-references:** ADR-0011 (DB pool architecture), spec §5.1
(field table with audit context), plan task 2 (Settings expansion
implementation).
