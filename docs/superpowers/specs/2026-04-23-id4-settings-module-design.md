# ID4 — Settings Module Design Spec

**Date:** 2026-04-23
**Author:** Orchestrator (Karl Raulerson) + AI agent
**Status:** Approved — ready for implementation plan
**Milestone:** B (Construction), Build Loop 3
**Target module:** `src/orchestrator/core/settings.py`
**Brainstorming session:** 2026-04-23 (14 decisions locked)

---

## 1. Purpose & scope

ID4 is the typed configuration module every later Milestone B+ feature reads through (DB pool, FastAPI app, Steam/Epic adapters, validator, scheduler, CLI). It is **load-bearing**: a field added here is referenced by every downstream consumer; a regression here is felt everywhere.

### In scope for BL3
- `pydantic-settings`-based `Settings` class with 16 typed fields.
- Singleton accessor via `@lru_cache get_settings()`.
- Whitespace-strip + min-length validation for the single `SecretStr` field (`orchestrator_token`).
- Source resolution from env → `.env` → `/run/secrets` → defaults, default pydantic-settings order.
- Four diagnostic warnings emitted at construction (shadow, non-loopback host, wildcard CORS, over-Spike-F concurrency).
- Test suite in `tests/core/test_settings.py` with shared autouse isolation fixture in `tests/core/conftest.py`.
- Documentation: ADR-0010, CHANGELOG `[Unreleased]` entry, FEATURES.md Feature 3.

### Out of scope for BL3
- Docker Compose artifact updates (deferred to BL5 or a separate compose PR).
- Re-wiring ID1 migration runner to read `require_local_fs` from `get_settings()` (filed as SEV-4 follow-up).
- API/consumer-side validation of settings (belongs to BL4/BL5 in their own Build Loops).
- Filesystem existence checks for paths (consumers check at their own init).
- Container-start failure handling (BL5 concern).

---

## 2. Locked decisions (Q1–Q13)

| # | Area | Decision |
|---|---|---|
| 1 | Scope tier | **C-committed** — 16 runtime fields + `secrets_dir` in `model_config`. Speculative fields (APScheduler, adapter timeouts, validator interval) deferred until their owning features are designed. |
| 2 | Shape | **Flat** single `Settings` class; no nested sub-models. |
| 3 | Source order | Default pydantic-settings order (init > env > `.env` > secrets > defaults), plus a shadow warning when env `ORCH_TOKEN` AND file `/run/secrets/orchestrator_token` both exist. |
| 4 | Lifecycle | `@lru_cache` on `get_settings()`; lazy first-call instantiation. `reload_settings()` for tests/long-running escape. |
| 5 | `.env` policy | Load from CWD if present, silent if missing. Test fixture chdirs to `tmp_path` to block host-developer `.env` discovery. |
| 6 | Secrets scope | `orchestrator_token` is the single `SecretStr`. Other 15 fields are plain types. Platform credentials (Steam/Epic) never enter settings — stdin → session file only per Bible §7.2. |
| 7 | `secrets_dir` default | `/run/secrets` (single path). Tests override via `Settings(_secrets_dir=tmp_path)` kwarg. |
| 8 | Redaction strategy | Trust `pydantic.SecretStr`; verify via three regression assertions (`repr`, `model_dump()`, `model_dump(mode="json")`). |
| 9 | Required fields | `orchestrator_token` only (no default). Other 15 have Bible-sourced defaults. |
| 10 | Validation scope | Field-shape only (no filesystem checks). Plus four semantic warnings. |
| 11 | Env prefix | `ORCH_`. `orchestrator_token` uses `validation_alias=AliasChoices("ORCH_TOKEN", "orchestrator_token")` so env is `ORCH_TOKEN` while secrets-file name remains `orchestrator_token` (Bible §7.3 verbatim). |
| 12 | Test isolation | **Hybrid**: autouse `_isolated_env` fixture for source-resolution tests (scrub `ORCH_*`, chdir `tmp_path`, `cache_clear`); kwarg injection for pure validator tests. |
| 13 | Redaction assertions | Parameterized over 5 token shapes: 32-char alphanumeric, 64-char hex, base64+padding, embedded newline, non-ASCII unicode. |
| 14 | `lancache_nginx_cache_path` default | `/data/cache/cache/` — mirrors Lancache's internal container path per `project_lancache_deployment_params` memory. |

---

## 3. Module shape

### File: `src/orchestrator/core/settings.py` (~120 LoC target)

Public surface:

```python
class Settings(BaseSettings): ...

@lru_cache
def get_settings() -> Settings: ...

def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
```

Imports allowed:
- Standard library: `os`, `pathlib.Path`, `functools.lru_cache`, `typing.Literal`, `typing.Any`, `re`
- Third-party: `pydantic` (`Field`, `SecretStr`, `AliasChoices`, `field_validator`, `model_validator`); `pydantic_settings` (`BaseSettings`, `SettingsConfigDict`)
- First-party: `orchestrator.core.logging` (`get_logger`)

### Integration with ID3 logging
- Warning emits use `orchestrator.core.logging.get_logger(__name__)`.
- Timing: post-init model validator, runs once per `Settings()` instantiation → once per container start (since `get_settings()` caches).
- Events emitted: `config.secret_shadowed_by_env`, `config.api_bound_non_loopback`, `config.cors_wildcard`, `config.chunk_concurrency_unvalidated`.
- Always at WARNING level.

### Integration with ID1 migrations
- ID1 currently reads `ORCH_REQUIRE_LOCAL_FS` directly via `os.environ.get`. BL3 does **not** rewire ID1.
- A SEV-4 follow-up issue is filed at BL3 close.

---

## 4. Field inventory (16)

### Core / API

| Field | Type | Default | Env var | Validators |
|---|---|---|---|---|
| `orchestrator_token` | `SecretStr` | **required** | `ORCH_TOKEN` (alias) | `.strip()` via `@field_validator(mode="before")`; `min_length=32` after strip |
| `api_host` | `str` | `"127.0.0.1"` | `ORCH_API_HOST` | non-empty; warn if not in `{"127.0.0.1", "::1", "localhost"}` |
| `api_port` | `int` | `8765` | `ORCH_API_PORT` | `1 <= x <= 65535` |
| `cors_origins` | `list[str]` | `[]` | `ORCH_CORS_ORIGINS` (JSON list) | all elements non-empty strings; warn if `"*"` in list |
| `log_level` | `Literal["DEBUG","INFO","WARNING","ERROR","CRITICAL"]` | `"INFO"` | `ORCH_LOG_LEVEL` | (enum) |

### Database & migrations

| Field | Type | Default | Env var | Validators |
|---|---|---|---|---|
| `database_path` | `Path` | `/var/lib/orchestrator/orchestrator.db` | `ORCH_DATABASE_PATH` | none |
| `require_local_fs` | `Literal["strict","warn","off"]` | `"warn"` | `ORCH_REQUIRE_LOCAL_FS` | (enum) |

### Platform session paths

| Field | Type | Default | Env var |
|---|---|---|---|
| `steam_session_path` | `Path` | `/var/lib/orchestrator/steam_session.json` | `ORCH_STEAM_SESSION_PATH` |
| `epic_session_path` | `Path` | `/var/lib/orchestrator/epic_session.json` | `ORCH_EPIC_SESSION_PATH` |

### Lancache cache topology

| Field | Type | Default | Env var | Validators |
|---|---|---|---|---|
| `lancache_nginx_cache_path` | `Path` | `/data/cache/cache/` | `ORCH_LANCACHE_NGINX_CACHE_PATH` | none |
| `cache_slice_size_bytes` | `int` | `10_485_760` (10 MiB) | `ORCH_CACHE_SLICE_SIZE_BYTES` | `x > 0` |
| `cache_levels` | `str` | `"2:2"` | `ORCH_CACHE_LEVELS` | regex `^\d+(:\d+)*$` |
| `chunk_concurrency` | `int` | `32` | `ORCH_CHUNK_CONCURRENCY` | `1 <= x <= 256`; warn if `> 32` |

### Miscellaneous

| Field | Type | Default | Env var | Validators |
|---|---|---|---|---|
| `manifest_size_cap_bytes` | `int` | `134_217_728` (128 MiB) | `ORCH_MANIFEST_SIZE_CAP_BYTES` | `x > 0` |
| `epic_refresh_buffer_sec` | `int` | `600` | `ORCH_EPIC_REFRESH_BUFFER_SEC` | `x >= 0` |
| `steam_upstream_silent_days` | `int` | `15` | `ORCH_STEAM_UPSTREAM_SILENT_DAYS` | `x >= 1` |

**Total: 16 fields. 1 `SecretStr`, 15 plain.**

---

## 5. Source resolution & warning mechanics

### Model config

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir="/run/secrets",
        extra="ignore",            # unknown ORCH_* vars don't crash — forward compat
        case_sensitive=False,
    )
```

### Default source order (no customization)

```
init kwargs  >  env vars  >  .env file  >  secrets_dir files  >  field defaults
```

### Token alias + whitespace strip

```python
orchestrator_token: SecretStr = Field(
    ...,
    validation_alias=AliasChoices("ORCH_TOKEN", "orchestrator_token"),
    min_length=32,
)

@field_validator("orchestrator_token", mode="before")
@classmethod
def _strip_token(cls, v: Any) -> Any:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, SecretStr):
        return SecretStr(v.get_secret_value().strip())
    return v
```

`AliasChoices("ORCH_TOKEN", "orchestrator_token")` is required because env lookup uses `ORCH_TOKEN` but the secrets-file lookup uses the file's name literal (`orchestrator_token`, matching Bible §7.3). Pydantic-settings tries aliases in order but the underlying source precedence (env > file) is preserved.

### Warning emission

**Implementation note — effective `secrets_dir` resolution:** the shadow-warning check must read the *effective* `secrets_dir` at instantiation (which may come from `_secrets_dir=` kwarg at test time, env override, or `model_config` default). Reading `self.model_config["secrets_dir"]` misses the kwarg path and breaks the `TestWarnings[shadow]` case. The implementation captures the effective value during `__init__` via `settings_customise_sources` (or the pydantic-settings v2 equivalent — to be confirmed via Context7 at implementation time) and stores it on a private attribute `self._effective_secrets_dir` for the warning validator to read. This is the single subtle implementation point where Context7 verification is required before first commit.

```python
@model_validator(mode="after")
def _emit_config_warnings(self) -> "Settings":
    log = get_logger(__name__)

    # 1. Shadow warning (Q3) — uses effective secrets_dir (see implementation note above)
    secrets_dir = getattr(self, "_effective_secrets_dir", None)
    if secrets_dir:
        secret_file = Path(secrets_dir) / "orchestrator_token"
        if "ORCH_TOKEN" in os.environ and secret_file.is_file():
            log.warning("config.secret_shadowed_by_env",
                        secret_file=str(secret_file))

    # 2. Non-loopback host (Q10.C)
    if self.api_host not in {"127.0.0.1", "::1", "localhost"}:
        log.warning("config.api_bound_non_loopback",
                    api_host=self.api_host)

    # 3. Wildcard CORS
    if "*" in self.cors_origins:
        log.warning("config.cors_wildcard")

    # 4. Over-Spike-F concurrency
    if self.chunk_concurrency > 32:
        log.warning("config.chunk_concurrency_unvalidated",
                    chunk_concurrency=self.chunk_concurrency,
                    spike_f_validated_at=32)

    return self
```

### Required-field failure mode

Pydantic's default `ValidationError` for missing `orchestrator_token` is readable and precise. BL3 does not wrap it in a custom exception; the container-start handler (BL5) catches `ValidationError` at entry point, emits `CRITICAL`, exits 1 per Bible §7.3.

---

## 6. Test strategy

### `tests/core/conftest.py` — shared fixtures

```python
import os
import pytest
from orchestrator.core.settings import get_settings

VALID_TOKEN = "a" * 32  # 32-char minimum

@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("ORCH_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

@pytest.fixture
def secrets_dir(tmp_path):
    d = tmp_path / "run_secrets"
    d.mkdir()
    return d
```

### `tests/core/test_settings.py` — ~47 tests across 8 classes

| # | Class | Tests | Purpose |
|---|---|---|---|
| 1 | `TestRequiredFields` | 2 | Missing token → `ValidationError`; present → OK |
| 2 | `TestDefaults` | 1 parametrized over all 15 optional fields | Defaults match §4 table |
| 3 | `TestFieldValidators` | ~12 | Boundary/invalid rejections for all validated fields |
| 4 | `TestSourcePrecedence` | 5 | Env > .env > secrets-file > default; init kwargs beat all; `extra="ignore"` |
| 5 | `TestSecretLoading` | 3 | env works; file works; both missing → `ValidationError` |
| 6 | `TestRedaction` | 15 (3 × 5 shapes) | Q13 parametrize; raw NOT IN `repr`/`model_dump()`/`model_dump(mode="json")` |
| 7 | `TestWarnings` | 4 | All 4 warnings fire under expected conditions (captured via structlog test helper consistent with ID3) |
| 8 | `TestSingleton` | 2 | `get_settings()` returns same instance; `reload_settings()` refreshes |

**Coverage target:** 100% branch coverage on `settings.py`. The module has bounded branching; missed coverage indicates a missed test.

### TDD gate
1. Write test file + fixtures; commit.
2. Run `pytest tests/core/test_settings.py` — confirm **all tests fail** (import error until module exists, then `AttributeError`/`ValidationError` mismatches).
3. Mark `build_loop:tests_written` + `tests_verified_failing`.
4. Pause for Orchestrator review before writing implementation.
5. Implement `settings.py`; re-run until green.
6. Mark `build_loop:implemented`.

---

## 7. Security audit dimensions (for BL3 Phase 2.4)

Planned sub-agent targets (per CLAUDE.md "Multi-Agent Parallelism"):
- **SAST (Semgrep):** custom rules verify no `requests`/`urllib` in module (none needed); OWASP rules verify no secret-leak patterns.
- **Threat model cross-check:** TM-001 (bearer leak), TM-012 (log redaction) — the module is directly responsible; TM-004 (session file tamper) — out of scope.
- **Data isolation:** Settings has no DB access; trivially isolated.
- **Input validation:** every `ORCH_*` source is typed; `extra="ignore"` prevents unknown-var injection.
- **Logging:** verify the 4 warning events redact per Bible §8.6; token is `SecretStr` so never logged.

---

## 8. Documentation deliverables

### `docs/ADR documentation/0010-settings-module-design.md`
- **Status:** Accepted
- **Context:** Every Milestone B+ feature reads config through ID4.
- **Decision:** flat 16-field `BaseSettings`, `ORCH_` prefix, default source order with shadow warning, `@lru_cache` singleton.
- **Alternatives considered:** nested sub-models; secrets-file-above-env order; module-level singleton; `LANCACHE_` prefix.
- **Consequences:** adding a field requires a Bible cross-reference; adding a secret requires promoting the Q8 redaction tests.
- **References:** Bible §2, §7.3, §8; this spec.

### `CHANGELOG.md` — `[Unreleased]` entry
- **Security:** redaction verified at 3 serialization surfaces; whitespace-strip + min-length-32; shadow warning.
- **Added:** core settings module, `get_settings()`, `reload_settings()`, 47 tests.
- **Infrastructure:** `tests/core/conftest.py` autouse fixture.
- **Documentation:** ADR-0010, FEATURES.md Feature 3, this spec.

### `FEATURES.md` — Feature 3 (match ID1/ID3 structure)
Sections: What it does / Environment variables / Public API / Related artifacts / Known limitations (names the ID1-rewire follow-up).

### `PROJECT_BIBLE.md`
§11.1 ADR registry row for 0010 if that section tracks sequential ADRs. No content changes to §2 / §7 / §8 (they already commit to pydantic-settings + redaction).

---

## 9. Follow-up issues filed at BL3 close

1. **SEV-4** — Rewire ID1 migration runner to read `require_local_fs` from `get_settings()` instead of `os.environ.get`. One-file change; not a bug, just de-duplicating the source of truth.
2. **SEV-4** — Promote redaction tests to parameterize over every declared `SecretStr` field when BL future adds a 2nd secret (F1/F2 timeframe).
3. **SEV-3** — README section documenting the `ORCH_*` env var reference table for operators. Candidate for BL5 or standalone docs PR.

---

## 10. Memory artifact

At BL3 close, save a single memory: `project_bl3_settings_complete.md` containing commit hashes, one-line summaries of the 14 locked decisions, and any implementation-time surprises.

No per-decision memories; this spec is the authoritative record.

---

## 11. Commit plan for BL3 (for writing-plans phase)

Anticipated commit sequence (actual structure decided via A/B/C approval at each commit per `feedback_commit_approval`):

1. `docs(spec): ID4 settings module design (this file)` — after Orchestrator approves this spec.
2. `test(core): settings module failing test suite` — after TDD gate step 2.
3. `feat(core): ID4 settings module — pydantic-settings BaseSettings, @lru_cache get_settings, 4 config warnings` — after implementation green.
4. `docs(adr,changelog,features): ADR-0010 + CHANGELOG + FEATURES.md Feature 3` — after documentation drafts.
5. (Possible) `chore(issues): file SEV-3/SEV-4 follow-ups for BL3` — if the issues are filed locally before close.

Each commit presents A/B/C structure options (single bundled / multi-commit / squashed) before firing.

---

## 12. Definition of done (BL3)

- [ ] All 47 tests pass.
- [ ] 100% branch coverage on `settings.py`.
- [ ] Security re-audit sub-agent returns no SEV-1/SEV-2 findings.
- [ ] ADR-0010 + CHANGELOG + FEATURES.md Feature 3 committed.
- [ ] 2–3 follow-up issues filed on GitHub.
- [ ] `scripts/test-gate.sh --record-feature "ID4-settings"` runs.
- [ ] `scripts/process-checklist.sh --complete-step build_loop:feature_recorded` runs.
- [ ] Memory `project_bl3_settings_complete.md` written.
- [ ] Branch pushed + PR opened (Orchestrator approves push and merges on GitHub per `feedback_pr_merge_ownership`).
