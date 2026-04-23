# ID4 Settings Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/orchestrator/core/settings.py` — the typed configuration module every later Milestone B+ feature reads through. Delivers 16 typed fields (1 `SecretStr`, 15 plain), a `@lru_cache` singleton accessor, whitespace-strip + min-length validation on the bearer token, source resolution from env/`.env`/`/run/secrets`, and four diagnostic warnings fired at construction.

**Architecture:** Single `BaseSettings` subclass, flat field layout (no nested sub-models), default pydantic-settings source order (init > env > `.env` > secrets > defaults). Token alias via `AliasChoices` so env is `ORCH_TOKEN` while secrets-file remains `orchestrator_token` (Bible §7.3). Warnings emitted via `@model_validator(mode="after")` using the already-live `orchestrator.core.logging` logger from ID3. Test isolation via an autouse `_isolated_env` fixture that scrubs `ORCH_*`, chdirs `tmp_path`, and clears the `get_settings()` cache.

**Tech Stack:** Python 3.12, pydantic 2.x, pydantic-settings 2.x, pytest, pytest-asyncio, structlog (via ID3). No new top-level dependencies — pydantic-settings is already in `requirements.txt`.

**Reference spec:** `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`

---

## File structure

### Created
- `src/orchestrator/core/settings.py` — the module itself (~120 LoC)
- `tests/core/conftest.py` — shared autouse isolation fixture
- `tests/core/test_settings.py` — ~47 tests across 8 classes
- `docs/ADR documentation/0010-settings-module-design.md` — decision record

### Modified
- `CHANGELOG.md` — `[Unreleased]` entry (Security / Added / Infrastructure / Documentation)
- `FEATURES.md` — new Feature 3 section matching ID1/ID3 structure
- `.claude/process-state.json` — auto-updated by `scripts/process-checklist.sh --complete-step`
- `.claude/tool-usage.json` — auto-updated by framework
- `.claude/build-progress.json` — auto-updated by `scripts/test-gate.sh --record-feature`

### Not modified this BL
- `src/orchestrator/db/migrate.py` — ID1 continues to read `ORCH_REQUIRE_LOCAL_FS` directly; rewire deferred to SEV-4 follow-up
- `Dockerfile`, `docker-compose.yml` — compose integration deferred
- `src/orchestrator/__init__.py` — no new exports from package root

---

## Task decomposition overview

| Task | Theme | Checklist step fired at end |
|---|---|---|
| 1 | Shared test fixture (`conftest.py`) | — |
| 2 | Failing test suite (all 8 classes) | — |
| 3 | Verify tests fail + commit | `tests_written`, `tests_verified_failing` |
| 4 | Skeleton: model_config + fields + defaults | — |
| 5 | Token handling: alias + strip validator | — |
| 6 | Singleton accessor: `get_settings` + `reload_settings` | — |
| 7 | Warning emission validator | — |
| 8 | Full suite green + coverage check + commit | `implemented` |
| 9 | Parallel security re-audit sub-agents + commit | `security_audit` |
| 10 | ADR-0010 + CHANGELOG + FEATURES + commit | `documentation_updated` |
| 11 | File follow-up issues + record feature + PR | `feature_recorded` |

---

## Task 1: Shared test fixture

**Files:**
- Create: `tests/core/conftest.py`

- [ ] **Step 1: Verify tests/core dir exists and list existing content**

Run: `ls tests/core/`

Expected: directory exists (created during Phase 2 scaffold). If empty, that's fine.

- [ ] **Step 2: Write conftest.py**

Create `tests/core/conftest.py`:

```python
"""Shared fixtures for orchestrator.core tests.

Provides environment isolation so tests never inherit host-developer
ORCH_* env vars or a project-root .env file. Every test starts from a
clean slate; individual tests opt in to specific values via
monkeypatch.setenv or explicit Settings(...) kwargs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestrator.core.settings import get_settings


VALID_TOKEN = "a" * 32  # 32-character minimum for ORCH_TOKEN


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Scrub ORCH_* env vars, chdir to tmp_path (blocks host .env
    discovery), and clear the get_settings() cache. Runs before every
    test in tests/core/.
    """
    for key in list(os.environ):
        if key.startswith("ORCH_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    """Returns a freshly-created directory suitable for use as a
    Settings(_secrets_dir=...) override or a
    monkeypatch.setitem(Settings.model_config, "secrets_dir", ...) target.
    """
    directory = tmp_path / "run_secrets"
    directory.mkdir()
    return directory
```

- [ ] **Step 3: Verify file exists**

Run: `test -f tests/core/conftest.py && echo OK`
Expected: `OK`

Note: Do NOT run pytest yet — `get_settings` doesn't exist, so the import in conftest will fail. This is expected until Task 2 writes tests and Task 4 creates the module.

---

## Task 2: Failing test suite (all 8 test classes)

**Files:**
- Create: `tests/core/test_settings.py`

- [ ] **Step 1: Write the complete test file**

Create `tests/core/test_settings.py`:

```python
"""Tests for orchestrator.core.settings.

Covers:
  1. Required fields (orchestrator_token is the only required field)
  2. Defaults (all 15 optional fields match Bible-sourced values)
  3. Field validators (boundary and type rejections)
  4. Source precedence (init > env > .env > secrets > default)
  5. Secret loading (env path, file path, both missing)
  6. Redaction (raw token never appears in repr/model_dump forms)
  7. Warning emission (4 diagnostic warnings)
  8. Singleton behavior (get_settings / reload_settings)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from orchestrator.core.settings import Settings, get_settings, reload_settings

from tests.core.conftest import VALID_TOKEN


# ----------------------------------------------------------------------
# 1. Required fields
# ----------------------------------------------------------------------

class TestRequiredFields:
    def test_missing_token_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            Settings()
        assert "orchestrator_token" in str(exc_info.value).lower() or \
               "token" in str(exc_info.value).lower()

    def test_present_token_constructs(self):
        settings = Settings(orchestrator_token=VALID_TOKEN)
        assert settings.orchestrator_token.get_secret_value() == VALID_TOKEN


# ----------------------------------------------------------------------
# 2. Defaults
# ----------------------------------------------------------------------

class TestDefaults:
    @pytest.fixture
    def settings(self) -> Settings:
        return Settings(orchestrator_token=VALID_TOKEN)

    @pytest.mark.parametrize("field,expected", [
        ("api_host", "127.0.0.1"),
        ("api_port", 8765),
        ("cors_origins", []),
        ("log_level", "INFO"),
        ("database_path", Path("/var/lib/orchestrator/orchestrator.db")),
        ("require_local_fs", "warn"),
        ("steam_session_path", Path("/var/lib/orchestrator/steam_session.json")),
        ("epic_session_path", Path("/var/lib/orchestrator/epic_session.json")),
        ("lancache_nginx_cache_path", Path("/data/cache/cache/")),
        ("cache_slice_size_bytes", 10_485_760),
        ("cache_levels", "2:2"),
        ("chunk_concurrency", 32),
        ("manifest_size_cap_bytes", 134_217_728),
        ("epic_refresh_buffer_sec", 600),
        ("steam_upstream_silent_days", 15),
    ])
    def test_default_value(self, settings, field, expected):
        assert getattr(settings, field) == expected


# ----------------------------------------------------------------------
# 3. Field validators
# ----------------------------------------------------------------------

class TestFieldValidators:
    def test_token_too_short_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token="a" * 31)

    def test_token_exactly_32_accepts(self):
        Settings(orchestrator_token="a" * 32)

    def test_token_with_whitespace_stripped_to_32(self):
        raw = "  " + "x" * 32 + "\n"
        s = Settings(orchestrator_token=raw)
        assert s.orchestrator_token.get_secret_value() == "x" * 32

    def test_token_with_whitespace_below_32_after_strip_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token="  " + "x" * 30 + "  ")

    def test_api_port_zero_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, api_port=0)

    def test_api_port_65536_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, api_port=65536)

    def test_api_port_boundaries_accept(self):
        Settings(orchestrator_token=VALID_TOKEN, api_port=1)
        Settings(orchestrator_token=VALID_TOKEN, api_port=65535)

    def test_log_level_invalid_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, log_level="SILLY")

    def test_require_local_fs_invalid_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, require_local_fs="maybe")

    def test_cache_levels_invalid_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, cache_levels="2-2")

    def test_cache_levels_valid_accepts(self):
        Settings(orchestrator_token=VALID_TOKEN, cache_levels="1:1:1")

    def test_chunk_concurrency_zero_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, chunk_concurrency=0)

    def test_chunk_concurrency_257_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, chunk_concurrency=257)

    def test_cache_slice_size_zero_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, cache_slice_size_bytes=0)

    def test_manifest_size_cap_zero_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, manifest_size_cap_bytes=0)

    def test_cors_origins_empty_string_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, cors_origins=[""])


# ----------------------------------------------------------------------
# 4. Source precedence
# ----------------------------------------------------------------------

class TestSourcePrecedence:
    def test_init_kwargs_beat_env(self, monkeypatch):
        monkeypatch.setenv("ORCH_API_PORT", "9999")
        s = Settings(orchestrator_token=VALID_TOKEN, api_port=1234)
        assert s.api_port == 1234

    def test_env_beats_dotenv(self, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text("ORCH_API_PORT=1111\n")
        monkeypatch.setenv("ORCH_API_PORT", "2222")
        s = Settings(orchestrator_token=VALID_TOKEN)
        assert s.api_port == 2222

    def test_dotenv_beats_default(self, tmp_path):
        (tmp_path / ".env").write_text("ORCH_API_PORT=3333\n")
        s = Settings(orchestrator_token=VALID_TOKEN)
        assert s.api_port == 3333

    def test_unknown_orch_env_var_ignored(self, monkeypatch):
        monkeypatch.setenv("ORCH_SOMETHING_NEW", "hello")
        Settings(orchestrator_token=VALID_TOKEN)  # must not raise

    def test_secret_file_beats_default(self, secrets_dir, monkeypatch):
        (secrets_dir / "orchestrator_token").write_text("s" * 32)
        monkeypatch.setitem(Settings.model_config, "secrets_dir", str(secrets_dir))
        s = Settings()  # no kwargs, no env
        assert s.orchestrator_token.get_secret_value() == "s" * 32


# ----------------------------------------------------------------------
# 5. Secret loading
# ----------------------------------------------------------------------

class TestSecretLoading:
    def test_env_only(self, monkeypatch):
        monkeypatch.setenv("ORCH_TOKEN", "e" * 32)
        s = Settings()
        assert s.orchestrator_token.get_secret_value() == "e" * 32

    def test_secret_file_only(self, secrets_dir, monkeypatch):
        (secrets_dir / "orchestrator_token").write_text("f" * 32)
        monkeypatch.setitem(Settings.model_config, "secrets_dir", str(secrets_dir))
        s = Settings()
        assert s.orchestrator_token.get_secret_value() == "f" * 32

    def test_both_missing_raises(self):
        with pytest.raises(ValidationError):
            Settings()


# ----------------------------------------------------------------------
# 6. Redaction
# ----------------------------------------------------------------------

REDACTION_TOKEN_SHAPES = [
    pytest.param("a" * 32, id="alphanumeric"),
    pytest.param("0123456789abcdef" * 4, id="hex"),
    pytest.param("Zm9vYmFyYmF6" + "=" * 20, id="base64-padding"),
    pytest.param("x" * 16 + "\n" + "y" * 16, id="embedded-newline"),
    pytest.param("🔒secret-ünïcödé-token-padded-00", id="unicode"),
]


class TestRedaction:
    @pytest.mark.parametrize("raw", REDACTION_TOKEN_SHAPES)
    def test_raw_not_in_repr(self, raw):
        s = Settings(orchestrator_token=raw)
        # The stripped value is what actually lands on the field. Check
        # the stripped form isn't leaked either.
        stripped = raw.strip()
        assert stripped not in repr(s)
        assert raw not in repr(s)

    @pytest.mark.parametrize("raw", REDACTION_TOKEN_SHAPES)
    def test_raw_not_in_model_dump(self, raw):
        s = Settings(orchestrator_token=raw)
        stripped = raw.strip()
        dump_str = str(s.model_dump())
        assert stripped not in dump_str
        assert raw not in dump_str

    @pytest.mark.parametrize("raw", REDACTION_TOKEN_SHAPES)
    def test_raw_not_in_model_dump_json(self, raw):
        s = Settings(orchestrator_token=raw)
        stripped = raw.strip()
        as_json = json.dumps(s.model_dump(mode="json"), default=str)
        assert stripped not in as_json
        assert raw not in as_json


# ----------------------------------------------------------------------
# 7. Warning emission
# ----------------------------------------------------------------------

class TestWarnings:
    def test_shadow_warning_fires(self, monkeypatch, secrets_dir, caplog):
        (secrets_dir / "orchestrator_token").write_text("f" * 32)
        monkeypatch.setitem(Settings.model_config, "secrets_dir", str(secrets_dir))
        monkeypatch.setenv("ORCH_TOKEN", "e" * 32)
        with caplog.at_level("WARNING"):
            Settings()
        assert any(
            "secret_shadowed_by_env" in record.message or
            "secret_shadowed_by_env" in getattr(record, "event", "")
            for record in caplog.records
        )

    def test_non_loopback_host_warning_fires(self, caplog):
        with caplog.at_level("WARNING"):
            Settings(orchestrator_token=VALID_TOKEN, api_host="0.0.0.0")
        assert any(
            "api_bound_non_loopback" in record.message or
            "api_bound_non_loopback" in getattr(record, "event", "")
            for record in caplog.records
        )

    def test_wildcard_cors_warning_fires(self, caplog):
        with caplog.at_level("WARNING"):
            Settings(orchestrator_token=VALID_TOKEN, cors_origins=["*"])
        assert any(
            "cors_wildcard" in record.message or
            "cors_wildcard" in getattr(record, "event", "")
            for record in caplog.records
        )

    def test_over_spike_f_concurrency_warning_fires(self, caplog):
        with caplog.at_level("WARNING"):
            Settings(orchestrator_token=VALID_TOKEN, chunk_concurrency=64)
        assert any(
            "chunk_concurrency_unvalidated" in record.message or
            "chunk_concurrency_unvalidated" in getattr(record, "event", "")
            for record in caplog.records
        )


# ----------------------------------------------------------------------
# 8. Singleton behavior
# ----------------------------------------------------------------------

class TestSingleton:
    def test_get_settings_returns_same_instance(self, monkeypatch):
        monkeypatch.setenv("ORCH_TOKEN", "e" * 32)
        a = get_settings()
        b = get_settings()
        assert a is b

    def test_reload_settings_returns_fresh_instance(self, monkeypatch):
        monkeypatch.setenv("ORCH_TOKEN", "e" * 32)
        first = get_settings()
        monkeypatch.setenv("ORCH_API_PORT", "9000")
        second = reload_settings()
        assert first is not second
        assert second.api_port == 9000
```

- [ ] **Step 2: Verify file exists and has expected content**

Run: `wc -l tests/core/test_settings.py`
Expected: ~290-310 lines.

---

## Task 3: Verify tests fail + commit

- [ ] **Step 1: Attempt full test run — expect import failure**

Run: `pytest tests/core/test_settings.py -v 2>&1 | head -30`
Expected: `ImportError: cannot import name 'Settings' from 'orchestrator.core.settings'` (or similar — the module doesn't exist yet).

Document the exact failure mode in the task-close note. If the error is not an ImportError as expected, investigate before proceeding.

- [ ] **Step 2: Mark process checklist step**

Run: `scripts/process-checklist.sh --complete-step build_loop:tests_written`
Expected: `[OK] Step marked: tests_written`

Run: `scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing`
Expected: `[OK] Step marked: tests_verified_failing`

- [ ] **Step 3: Check commit structure options with Orchestrator**

PAUSE — ask Orchestrator for A/B/C commit structure options. Default proposal:

- **A.** Single commit: `conftest.py` + `test_settings.py` + `.claude/process-state.json` + `.claude/tool-usage.json`
- **B.** Split: tests in one commit, `.claude/*.json` in a trailing chore commit
- **C.** Bundle tests with impl (skip this commit; add tests to the Task 8 implementation commit)

Recommend **A** — atomic TDD checkpoint, framework state rolls in with the beat it belongs to.

- [ ] **Step 4: Commit per Orchestrator's pick**

Default command for option A:

```bash
git add tests/core/conftest.py tests/core/test_settings.py .claude/process-state.json .claude/tool-usage.json
git commit -m "$(cat <<'EOF'
test(core): ID4 settings — failing test suite (47 tests, 8 classes)

Covers required fields, defaults across all 15 optional fields,
field validators (boundaries + enum + regex rejections), source
precedence (init > env > .env > secrets > default),
secret-loading paths, redaction across 5 token shapes ×
3 serialization forms (15 assertions), all 4 diagnostic warnings,
and singleton behavior.

TDD gate: all tests fail with ImportError pre-implementation.
Verified via `pytest tests/core/test_settings.py -v`.

Process checklist: tests_written + tests_verified_failing marked.

Spec: docs/superpowers/specs/2026-04-23-id4-settings-module-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Pause for Orchestrator review per BL1/BL2 rhythm**

STOP — spec line 6 of the Build Loop says pause here before implementation. Post a one-line status to the Orchestrator: "Tests committed at <hash>. Ready to implement when you give the green light."

---

## Task 4: Skeleton — model_config + fields + defaults

**Files:**
- Create: `src/orchestrator/core/settings.py`

- [ ] **Step 1: Verify core package directory exists**

Run: `ls src/orchestrator/core/`
Expected: directory exists (contains `logging.py` from ID3).

- [ ] **Step 2: Write minimal module skeleton**

Create `src/orchestrator/core/settings.py`:

```python
"""Typed application configuration for the lancache orchestrator.

Every Milestone B+ feature reads config through this module via
`get_settings()`. Fields are loaded in default pydantic-settings order:
init kwargs > env vars > .env file > /run/secrets files > defaults.

The single SecretStr field (`orchestrator_token`) supports two
lookup names via AliasChoices: `ORCH_TOKEN` (env var) and
`orchestrator_token` (secrets-file name, matching Bible §7.3).

See docs/superpowers/specs/2026-04-23-id4-settings-module-design.md
for the full design rationale.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from orchestrator.core.logging import get_logger


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SPIKE_F_CHUNK_CONCURRENCY = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir="/run/secrets",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core / API --------------------------------------------------
    orchestrator_token: SecretStr = Field(
        ...,
        validation_alias=AliasChoices("ORCH_TOKEN", "orchestrator_token"),
        min_length=32,
    )
    api_host: str = Field(default="127.0.0.1", min_length=1)
    api_port: int = Field(default=8765, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=list)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Database & migrations --------------------------------------
    database_path: Path = Path("/var/lib/orchestrator/orchestrator.db")
    require_local_fs: Literal["strict", "warn", "off"] = "warn"

    # --- Platform session paths -------------------------------------
    steam_session_path: Path = Path("/var/lib/orchestrator/steam_session.json")
    epic_session_path: Path = Path("/var/lib/orchestrator/epic_session.json")

    # --- Lancache cache topology ------------------------------------
    lancache_nginx_cache_path: Path = Path("/data/cache/cache/")
    cache_slice_size_bytes: int = Field(default=10_485_760, gt=0)
    cache_levels: str = Field(default="2:2", pattern=r"^\d+(:\d+)*$")
    chunk_concurrency: int = Field(default=32, ge=1, le=256)

    # --- Misc --------------------------------------------------------
    manifest_size_cap_bytes: int = Field(default=134_217_728, gt=0)
    epic_refresh_buffer_sec: int = Field(default=600, ge=0)
    steam_upstream_silent_days: int = Field(default=15, ge=1)


# Placeholder exports — filled in subsequent tasks
def get_settings() -> Settings:  # type: ignore[misc]
    raise NotImplementedError


def reload_settings() -> Settings:
    raise NotImplementedError
```

- [ ] **Step 3: Run the tests that are reachable — expect the skeleton to satisfy some**

Run: `pytest tests/core/test_settings.py -v 2>&1 | tail -40`
Expected:
- `TestRequiredFields` — both tests pass
- `TestDefaults` — most pass (but `cors_origins` list equality might need verification)
- `TestFieldValidators` — most pass (the validation is already declared via `Field(...)` constraints), EXCEPT the two token-strip tests which still fail (no strip validator yet)
- `TestSourcePrecedence` — most pass (env and dotenv handled by BaseSettings); `test_secret_file_beats_default` should pass via model_config `/run/secrets` being absent
- `TestSecretLoading` — partial
- `TestRedaction` — will FAIL for shapes where the stripped form differs from raw (stripping isn't wired)
- `TestWarnings` — all 4 FAIL (no warning validator yet)
- `TestSingleton` — both FAIL (`get_settings` raises `NotImplementedError`)

Capture exact pass/fail count to confirm progress.

---

## Task 5: Token handling — alias + strip validator

- [ ] **Step 1: Add the strip validator**

Append to `src/orchestrator/core/settings.py` inside the `Settings` class, immediately after the field declarations:

```python
    @field_validator("orchestrator_token", mode="before")
    @classmethod
    def _strip_token(cls, v: Any) -> Any:
        """Strip whitespace before min_length runs. Bible §7.3."""
        if isinstance(v, SecretStr):
            return SecretStr(v.get_secret_value().strip())
        if isinstance(v, str):
            return v.strip()
        return v
```

- [ ] **Step 2: Run validator + redaction tests**

Run: `pytest tests/core/test_settings.py::TestFieldValidators tests/core/test_settings.py::TestRedaction -v`
Expected: all pass. If the `test_token_with_whitespace_below_32_after_strip_rejects` test fails, the strip validator isn't running before `min_length` — verify `mode="before"`.

---

## Task 6: Singleton accessors

- [ ] **Step 1: Replace the placeholder `get_settings` + `reload_settings`**

In `src/orchestrator/core/settings.py`, replace the two placeholder functions with:

```python
@lru_cache
def get_settings() -> Settings:
    """Lazy singleton accessor. First call constructs; subsequent
    calls return the cached instance. Tests clear via
    `get_settings.cache_clear()` in the `_isolated_env` autouse fixture.
    """
    return Settings()


def reload_settings() -> Settings:
    """Force a fresh instantiation — primarily for tests or for a
    future SIGHUP-style config reload. Clears the `get_settings`
    cache and returns a freshly-built instance.
    """
    get_settings.cache_clear()
    return get_settings()
```

- [ ] **Step 2: Run singleton tests**

Run: `pytest tests/core/test_settings.py::TestSingleton -v`
Expected: both pass.

---

## Task 7: Warning emission validator

- [ ] **Step 1: Add the `_emit_config_warnings` model validator**

Append inside the `Settings` class, after `_strip_token`:

```python
    @model_validator(mode="after")
    def _emit_config_warnings(self) -> "Settings":
        """Emit diagnostic WARNINGs for non-fatal but notable config
        states: secret shadowed by env, non-loopback api_host,
        wildcard CORS, over-Spike-F chunk concurrency.
        """
        log = get_logger(__name__)

        # 1. Shadow warning — env and secret-file both set
        secrets_dir = self.model_config.get("secrets_dir")
        if secrets_dir:
            secret_file = Path(secrets_dir) / "orchestrator_token"
            if "ORCH_TOKEN" in os.environ and secret_file.is_file():
                log.warning(
                    "config.secret_shadowed_by_env",
                    secret_file=str(secret_file),
                )

        # 2. Non-loopback host
        if self.api_host not in _LOOPBACK_HOSTS:
            log.warning(
                "config.api_bound_non_loopback",
                api_host=self.api_host,
            )

        # 3. Wildcard CORS
        if "*" in self.cors_origins:
            log.warning("config.cors_wildcard")

        # 4. Over-Spike-F concurrency
        if self.chunk_concurrency > _SPIKE_F_CHUNK_CONCURRENCY:
            log.warning(
                "config.chunk_concurrency_unvalidated",
                chunk_concurrency=self.chunk_concurrency,
                spike_f_validated_at=_SPIKE_F_CHUNK_CONCURRENCY,
            )

        return self
```

- [ ] **Step 2: Run warning tests**

Run: `pytest tests/core/test_settings.py::TestWarnings -v`
Expected: all 4 pass.

If `test_shadow_warning_fires` fails, double-check the test uses `monkeypatch.setitem(Settings.model_config, "secrets_dir", ...)` rather than `Settings(_secrets_dir=...)`. Per Context7 v2 docs, the kwarg override doesn't propagate to `model_config`; the monkeypatch pattern is the one the validator can see.

---

## Task 8: Full suite green + coverage + commit

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/core/test_settings.py -v 2>&1 | tail -20`
Expected: ~47 tests collected, all pass.

- [ ] **Step 2: Run coverage on settings.py**

Run: `pytest tests/core/test_settings.py --cov=src/orchestrator/core/settings --cov-report=term-missing`
Expected: 100% branch coverage on `settings.py`. Any uncovered line is a missing test case — add the test, don't lower the target.

- [ ] **Step 3: Run project-wide test suite to verify no regressions**

Run: `pytest tests/ -x --tb=short 2>&1 | tail -10`
Expected: all ~159 tests (112 pre-BL3 + 47 new) pass.

- [ ] **Step 4: Run lint + type check**

Run: `ruff check src/orchestrator/core/settings.py tests/core/`
Expected: no issues.

Run: `mypy --strict src/orchestrator/core/settings.py`
Expected: no issues.

- [ ] **Step 5: Mark process checklist**

Run: `scripts/process-checklist.sh --complete-step build_loop:implemented`
Expected: `[OK] Step marked: implemented`

- [ ] **Step 6: Present commit options and commit per Orchestrator pick**

Default A/B/C proposal:

- **A.** Single commit: `src/orchestrator/core/settings.py` + `.claude/process-state.json`
- **B.** Single commit: implementation only; `.claude/*.json` rolls into the next commit
- **C.** Split across Tasks 4/5/6/7 into four commits (one per feature beat)

Recommend **A** — one implementation commit is the convention established by ID1 and ID3.

Command for option A:

```bash
git add src/orchestrator/core/settings.py .claude/process-state.json .claude/tool-usage.json
git commit -m "$(cat <<'EOF'
feat(core): ID4 settings module — pydantic-settings BaseSettings

Implements src/orchestrator/core/settings.py with 16 typed fields
(1 SecretStr + 15 plain). ORCH_ env prefix; orchestrator_token
loads via AliasChoices from env (ORCH_TOKEN) or /run/secrets
(orchestrator_token file). Default pydantic-settings source
order; whitespace-strip + min-length-32 validator on the token.

@lru_cache get_settings() singleton; reload_settings() escape hatch.

Four @model_validator(mode="after") diagnostic warnings:
  - config.secret_shadowed_by_env (env + file both set)
  - config.api_bound_non_loopback (api_host not loopback)
  - config.cors_wildcard ("*" in cors_origins)
  - config.chunk_concurrency_unvalidated (> Spike-F 32)

47 tests passing, 100% branch coverage on settings.py.
Full suite green (159 tests).

Spec: docs/superpowers/specs/2026-04-23-id4-settings-module-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Parallel security re-audit

- [ ] **Step 1: Dispatch 3 parallel audit sub-agents**

Per CLAUDE.md "Multi-Agent Parallelism" for Phase 2.4, dispatch three sub-agents in a single message (use `superpowers:dispatching-parallel-agents`). Each gets a focused, self-contained prompt.

**Agent 1 — SAST**: "Run `semgrep --config=auto --config=.semgrep/ src/orchestrator/core/settings.py` and interpret findings. Also run project custom-rule set under `.semgrep/`. Report any MEDIUM-or-higher findings with concrete attack scenarios. Ignore deprecated-API findings unless exploitable."

**Agent 2 — Threat model cross-check**: "Review src/orchestrator/core/settings.py against the Bible threat model (docs/phase-1/threat-model.md) for TM-001 (bearer-token leak), TM-012 (log redaction), and any data-isolation concerns. Verify the 4 warning emissions don't themselves leak sensitive content. Report concrete exploit paths only; abstract threats should be discarded."

**Agent 3 — Input validation + redaction**: "Audit src/orchestrator/core/settings.py for input-validation gaps: can any ORCH_* env var inject shell metacharacters, path traversal, or ReDoS? Verify the cache_levels regex is linear-time. Verify the SecretStr redaction holds across repr(), model_dump(), and model_dump(mode='json'). Run `python -c 'from orchestrator.core.settings import Settings; print(repr(Settings(orchestrator_token=\"x\"*32)))'` and confirm no raw token appears."

- [ ] **Step 2: Consolidate findings**

Summarize all 3 sub-agent reports. Triage:
- SEV-1/SEV-2 → must fix before marking `security_audit`
- SEV-3/SEV-4 → file GitHub issues, defer

- [ ] **Step 3: Address any SEV-1/SEV-2 findings test-first**

If findings exist, for each: write a failing test that demonstrates the issue, fix, verify green.

- [ ] **Step 4: Mark process checklist**

Run: `scripts/process-checklist.sh --complete-step build_loop:security_audit`
Expected: `[OK] Step marked: security_audit`

- [ ] **Step 5: Commit fixes (if any)**

If no SEV-1/2 findings were fixed, skip the commit. Otherwise present A/B/C options and commit per Orchestrator's pick.

---

## Task 10: Documentation

**Files:**
- Create: `docs/ADR documentation/0010-settings-module-design.md`
- Modify: `CHANGELOG.md`
- Modify: `FEATURES.md`

- [ ] **Step 1: Write ADR-0010**

Check ADR template: `ls templates/adr* 2>/dev/null || ls docs/ADR\ documentation/0008-*.md` (to follow existing structure).

Create `docs/ADR documentation/0010-settings-module-design.md`:

```markdown
# ADR-0010: Settings module design

**Status:** Accepted
**Date:** 2026-04-23
**Builder:** Karl Raulerson (Orchestrator) + AI agent
**Related:** ADR-0001 (arch), ADR-0008 (migrations package-data), ADR-0009 (logging)

## Context

Every feature in Milestone B+ (DB pool, FastAPI app, Steam/Epic
adapters, validator, scheduler, CLI) reads configuration through
a single typed module. This module is load-bearing: a field
added here is referenced by every downstream consumer; a
regression here is felt everywhere.

The Project Bible §2 pre-commits the stack to `pydantic` v2 +
`pydantic-settings`. Bible §7.3 commits the bearer token to a
Docker secret at `/run/secrets/orchestrator_token`, minimum 32
characters, container refuses to start if missing. Bible §8
commits to structured logging with secret redaction (TM-012).

The live question for BL3 was the shape, scope, precedence,
lifecycle, redaction strategy, and test surface of the module.

## Decision

- **Flat 16-field `BaseSettings`** (1 SecretStr + 15 plain). No
  nested sub-models. Deferred speculative fields (APScheduler,
  adapter timeouts, validator interval) to their owning features.
- **`ORCH_` env prefix**, matching the precedent set by ID1's
  `ORCH_REQUIRE_LOCAL_FS`. `orchestrator_token` uses
  `AliasChoices("ORCH_TOKEN", "orchestrator_token")` so env is
  `ORCH_TOKEN` while the secrets-file name remains
  `orchestrator_token` (Bible §7.3 verbatim).
- **Default pydantic-settings source order**: init kwargs > env
  vars > `.env` file > secrets_dir files > defaults. No
  `settings_customise_sources` override.
- **Shadow warning**: when env `ORCH_TOKEN` AND file
  `/run/secrets/orchestrator_token` both exist, emit a WARNING
  (`config.secret_shadowed_by_env`). Does not change behavior —
  only makes the silent-override diagnosable.
- **`@lru_cache` on `get_settings()`** for lazy singleton access.
  `reload_settings()` provided as an escape hatch for tests and
  future SIGHUP-style reloads.
- **`/run/secrets` default `secrets_dir`** (single path, Bible
  §7.3 verbatim). Tests override via
  `monkeypatch.setitem(Settings.model_config, "secrets_dir", ...)`.
- **Redaction**: trust `pydantic.SecretStr`; verify via three
  regression assertions (`repr`, `model_dump()`,
  `model_dump(mode="json")`) parameterized over five token
  shapes.
- **Field-shape validation only**. No filesystem checks. Four
  semantic warnings (shadow, non-loopback host, wildcard CORS,
  over-Spike-F chunk concurrency).

## Alternatives considered

- **Nested sub-models** (`Settings.api.port`, `Settings.steam.session_path`):
  rejected for env-var awkwardness (double-underscore delimiter),
  incompatibility with Docker-secret file naming, and premature
  scaling complexity at 16 fields.
- **Secrets-file-above-env source order**: rejected for inverting
  12-factor expectations. The shadow warning addresses the
  motivating concern without the inversion.
- **Module-level singleton** (`settings = Settings()`): rejected
  for import-time side effects. CLI `--help` and test collection
  would hit the required-token validator.
- **`LANCACHE_` env prefix**: rejected for confusability with
  Lancache (the nginx we orbit). `ORCH_` is unambiguous.
- **Explicit DI** (pass `Settings` through every consumer):
  rejected for boilerplate cost. `@lru_cache get_settings()` is
  the FastAPI idiom and testable via `cache_clear`.

## Consequences

- **Adding a field** requires a Bible cross-reference for the
  default, an entry in the `TestDefaults` parametrize list, and
  a FEATURES.md Feature 3 entry amendment if the field is
  operator-visible.
- **Adding a second SecretStr field** requires promoting the
  three redaction tests from `orchestrator_token`-specific to
  a field-introspection form that exercises every declared
  `SecretStr` field. This is tracked as a SEV-4 follow-up.
- **Bypassing `get_settings()`** (e.g., calling `Settings()`
  directly in a consumer) forfeits the singleton guarantee.
  Consumer code reviews should catch this.
- **ID1's `ORCH_REQUIRE_LOCAL_FS` direct os.environ read**
  remains in place for now; a SEV-4 follow-up rewires it to
  read from `get_settings()` for single-source-of-truth.

## References

- Bible §2, §7.3, §8
- Spec: `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`
- pydantic-settings v2 docs (Context7-verified)
```

- [ ] **Step 2: Update CHANGELOG.md**

Read the current `[Unreleased]` section: `grep -A 80 "## \[Unreleased\]" CHANGELOG.md | head -80`

Add under `[Unreleased]`:

```markdown
### Security
- Added whitespace-strip + min-length-32 validation for `ORCH_TOKEN`
  (Bible §7.3). Shadow warning emitted when env and `/run/secrets`
  both provide the token. Redaction verified at three serialization
  surfaces (`repr`, `model_dump`, `model_dump(mode="json")`) across
  five token shapes.

### Added
- Core settings module (`src/orchestrator/core/settings.py`) with
  16 typed fields covering API, database, platform session paths,
  Lancache cache topology, and observability.
- `orchestrator.core.settings.get_settings()` — lazy singleton
  accessor backed by `@lru_cache`.
- `orchestrator.core.settings.reload_settings()` — explicit reload
  escape hatch for tests and future SIGHUP-style config reloads.
- Four diagnostic WARNINGs emitted at settings construction:
  `config.secret_shadowed_by_env`, `config.api_bound_non_loopback`,
  `config.cors_wildcard`, `config.chunk_concurrency_unvalidated`.

### Infrastructure
- `tests/core/conftest.py` shared autouse fixture (`_isolated_env`)
  scrubs `ORCH_*` env vars and chdirs to `tmp_path` to prevent
  host-developer environment leakage into tests.
- `tests/core/test_settings.py` — 47 tests across 8 classes,
  100% branch coverage on settings.py.

### Documentation
- ADR-0010: Settings module design.
- FEATURES.md Feature 3: ID4 Settings module.
- Design spec at `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`.
```

- [ ] **Step 3: Update FEATURES.md**

Read the Feature 2 (ID3) section to match structure: `grep -A 60 "## Feature 2" FEATURES.md`

Append a new section:

```markdown
## Feature 3: ID4 — Settings Module

**Status:** Shipped in Milestone B Build Loop 3.
**Commit:** (fill in after implementation commit hash is known)
**Module:** `src/orchestrator/core/settings.py`

### What it does

Provides the typed configuration surface every Milestone B+
feature reads through. Loads values from four sources in
pydantic-settings default precedence (init kwargs > env vars >
`.env` > `/run/secrets` files > field defaults). Emits four
diagnostic WARNINGs at construction for non-fatal but notable
configuration states.

### Environment variables (`ORCH_*` prefix)

| Env var | Type | Default | Notes |
|---|---|---|---|
| `ORCH_TOKEN` (or secrets file `orchestrator_token`) | str (≥32 chars) | **required** | Bearer token; whitespace stripped |
| `ORCH_API_HOST` | str | `127.0.0.1` | Warns if not loopback |
| `ORCH_API_PORT` | int (1..65535) | `8765` | |
| `ORCH_CORS_ORIGINS` | JSON list | `[]` | Warns on `"*"` |
| `ORCH_LOG_LEVEL` | DEBUG/INFO/WARNING/ERROR/CRITICAL | `INFO` | |
| `ORCH_DATABASE_PATH` | Path | `/var/lib/orchestrator/orchestrator.db` | |
| `ORCH_REQUIRE_LOCAL_FS` | strict/warn/off | `warn` | Forwarded to ID1 migration runner |
| `ORCH_STEAM_SESSION_PATH` | Path | `/var/lib/orchestrator/steam_session.json` | |
| `ORCH_EPIC_SESSION_PATH` | Path | `/var/lib/orchestrator/epic_session.json` | |
| `ORCH_LANCACHE_NGINX_CACHE_PATH` | Path | `/data/cache/cache/` | Lancache container path |
| `ORCH_CACHE_SLICE_SIZE_BYTES` | int (>0) | 10 MiB | |
| `ORCH_CACHE_LEVELS` | str | `2:2` | nginx cache levels format |
| `ORCH_CHUNK_CONCURRENCY` | int (1..256) | `32` | Warns if > Spike-F 32 |
| `ORCH_MANIFEST_SIZE_CAP_BYTES` | int (>0) | 128 MiB | |
| `ORCH_EPIC_REFRESH_BUFFER_SEC` | int (≥0) | `600` | |
| `ORCH_STEAM_UPSTREAM_SILENT_DAYS` | int (≥1) | `15` | |

### Public API

```python
from orchestrator.core.settings import Settings, get_settings, reload_settings

settings = get_settings()  # @lru_cache singleton
token = settings.orchestrator_token.get_secret_value()
```

### Related artifacts

- Spec: `docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`
- ADR: `docs/ADR documentation/0010-settings-module-design.md`
- Tests: `tests/core/test_settings.py`

### Known limitations

- ID1's migration runner continues to read `ORCH_REQUIRE_LOCAL_FS`
  directly via `os.environ.get`. SEV-4 follow-up will rewire it
  through `get_settings()` for single-source-of-truth.
- Only `orchestrator_token` is `SecretStr`. When F1/F2 platform
  auth adds a 2nd `SecretStr` field, the three redaction tests
  will be promoted to parameterize over every declared
  `SecretStr` field.
```

- [ ] **Step 4: Verify CHANGELOG and FEATURES render**

Run: `head -40 CHANGELOG.md` and `grep -A 5 "Feature 3" FEATURES.md`
Expected: new sections visible and correctly placed.

- [ ] **Step 5: Mark process checklist**

Run: `scripts/process-checklist.sh --complete-step build_loop:documentation_updated`
Expected: `[OK] Step marked: documentation_updated`

- [ ] **Step 6: Present commit options and commit**

Default A/B/C proposal:

- **A.** Single commit: ADR + CHANGELOG + FEATURES + framework state.
- **B.** ADR in its own commit; CHANGELOG + FEATURES together.
- **C.** Three separate commits (ADR / CHANGELOG / FEATURES).

Recommend **A** — tightest atomic "documentation beat" per Bible rhythm.

Command for option A:

```bash
git add "docs/ADR documentation/0010-settings-module-design.md" CHANGELOG.md FEATURES.md .claude/process-state.json .claude/tool-usage.json
git commit -m "$(cat <<'EOF'
docs(adr,changelog,features): ID4 settings — ADR-0010 + Feature 3

ADR-0010 captures the 14 locked decisions, alternatives, and
consequences. CHANGELOG Unreleased gains Security / Added /
Infrastructure / Documentation entries. FEATURES.md Feature 3
enumerates the 16 env vars with defaults and validators, the
public API surface, related artifacts, and known limitations.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

After commit, backfill the Feature 3 "Commit:" field in FEATURES.md with the implementation hash from Task 8's commit and commit as a small follow-up (or amend per Orchestrator's preference — default is a small follow-up commit to avoid amending a pushed commit).

---

## Task 11: Follow-ups + record feature + PR

- [ ] **Step 1: File SEV-4 follow-up issue — ID1 rewire**

```bash
gh issue create \
  --title "SEV-4: Rewire ID1 migration runner to read require_local_fs from get_settings()" \
  --body "$(cat <<'EOF'
**Severity:** SEV-4 (enhancement)
**Discovered:** BL3 (ID4 settings module) — 2026-04-23
**Component:** `src/orchestrator/db/migrate.py`

## Context

ID1's migration runner reads `ORCH_REQUIRE_LOCAL_FS` directly via
`os.environ.get(...)`. BL3's `Settings` module now exposes
`require_local_fs` as a typed field. Rewiring reduces the
configuration sources of truth to one.

## Acceptance

- `src/orchestrator/db/migrate.py` reads `get_settings().require_local_fs` instead of `os.environ`.
- Existing migration tests pass unchanged.
- No behavior change; only source of truth change.

## Priority

SEV-4 — not a bug. Defer to a low-churn window.
EOF
)" \
  --label "sev-4,tech-debt,bl3-followup"
```

- [ ] **Step 2: File SEV-4 follow-up issue — redaction test promotion**

```bash
gh issue create \
  --title "SEV-4: Promote SecretStr redaction tests to field-introspection when 2nd secret field lands" \
  --body "$(cat <<'EOF'
**Severity:** SEV-4 (robustness)
**Discovered:** BL3 (ID4 settings module) — 2026-04-23
**Component:** `tests/core/test_settings.py::TestRedaction`

## Context

BL3 currently has a single SecretStr field (orchestrator_token).
The three redaction tests parameterize over five token shapes
but hard-code the field name. When F1/F2 platform auth adds a
second SecretStr field, the guard should promote to iterate
every declared SecretStr field in Settings.

## Acceptance

- Test iterates `Settings.model_fields` and asserts redaction
  for every field whose annotation is `SecretStr`.
- Exercised in `TestRedaction` once 2nd SecretStr field exists.

## Priority

SEV-4 — depends on F1/F2 landing. Don't do preemptively.
EOF
)" \
  --label "sev-4,robustness,bl3-followup"
```

- [ ] **Step 3: File SEV-3 follow-up issue — README ORCH_* reference**

```bash
gh issue create \
  --title "SEV-3: Add ORCH_* env var reference section to README" \
  --body "$(cat <<'EOF'
**Severity:** SEV-3 (documentation)
**Discovered:** BL3 (ID4 settings module) — 2026-04-23

## Context

FEATURES.md Feature 3 contains the env var reference table, but
operators typically look in README first. A short redirect-section
in README.md pointing at FEATURES.md Feature 3 (or duplicating
the key table) would improve discoverability.

## Acceptance

- README.md has a "Configuration" section linking to Feature 3
  or containing the environment-variable reference.

## Priority

SEV-3 — nice to have. Candidate for BL5 (when the first public-
facing deploy instructions are written) or a standalone docs PR.
EOF
)" \
  --label "sev-3,docs,bl3-followup"
```

- [ ] **Step 4: Record feature + reset test-gate counter**

Run: `scripts/test-gate.sh --record-feature "ID4-settings"`
Expected: `[OK] Feature recorded: ID4-settings (features_since_last_test=N)`

Run: `scripts/process-checklist.sh --complete-step build_loop:feature_recorded`
Expected: `[OK] Step marked: feature_recorded`

- [ ] **Step 5: Save BL3 summary memory**

Per CLAUDE.md "Qdrant Persistent Memory" — save a single project memory. Use `mcp__qdrant__qdrant-store` with:

**Content:** One-line summaries of the 14 locked decisions, the three implementation commits (spec / tests / impl / docs hashes), the three follow-up issue numbers, and any implementation-time surprise discovered during execution.

**Metadata:** `{"type": "project", "project": "lancache_orchestrator", "topic": "bl3-settings-complete", "milestone": "B", "phase": 2, "date": "2026-04-23"}`

- [ ] **Step 6: Update MEMORY.md index**

Write a new file `~/.claude/projects/-Users-karl-Documents-Claude-Projects-lancache-orchestrator/memory/project_bl3_settings_complete.md` with frontmatter and body mirroring the qdrant-store content. Add a one-line entry to the local MEMORY.md index:

```
- [BL3 settings complete](project_bl3_settings_complete.md) — ID4 shipped with 16 fields, 4 warnings, 47 tests at 100% coverage
```

- [ ] **Step 7: Commit any remaining framework state**

Run: `git status --short`

If `.claude/*.json` still shows modifications, present A/B/C and commit per pick. Default A (chore commit):

```bash
git add .claude/process-state.json .claude/tool-usage.json .claude/build-progress.json
git commit -m "$(cat <<'EOF'
chore(framework): close BL3 process checklist + reset test-gate counter

BL3 (ID4 settings) fully complete:
  tests_written ✓ tests_verified_failing ✓ implemented ✓
  security_audit ✓ documentation_updated ✓ feature_recorded ✓

test-gate.sh counter reset; features_since_last_test=1 (one
feature since UAT-1).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 8: Push branch + open PR**

Per `feedback_pr_merge_ownership`: open PR only, do not merge.

Per the hook advisory at session start: the branch currently has no upstream. First push:

```bash
git push -u origin feat/settings-module
```

Then open PR:

```bash
gh pr create --title "ID4 settings module — typed config + singleton + 4 warnings" \
  --body "$(cat <<'EOF'
## Summary
- Implements ID4 (BL3 of Milestone B): `src/orchestrator/core/settings.py` — the typed configuration module every later feature reads through.
- 16 fields (1 SecretStr + 15 plain), `@lru_cache get_settings()`, four diagnostic warnings, 47 tests at 100% branch coverage.
- Delivers ADR-0010, CHANGELOG entry, FEATURES.md Feature 3.
- Files three follow-up issues (SEV-4 ID1 rewire, SEV-4 redaction test promotion, SEV-3 README section).

## Spec
`docs/superpowers/specs/2026-04-23-id4-settings-module-design.md`

## Test plan
- [ ] Run `pytest tests/core/test_settings.py -v` — expect 47 passed
- [ ] Run `pytest tests/ --cov=src/orchestrator/core/settings` — expect 100% branch coverage on settings.py
- [ ] Run `ruff check src/orchestrator/core/settings.py tests/core/`
- [ ] Run `mypy --strict src/orchestrator/core/settings.py`
- [ ] Verify `scripts/test-gate.sh --check-batch` is clear
- [ ] Review ADR-0010, CHANGELOG Unreleased, FEATURES.md Feature 3
- [ ] Review the three SEV-3/SEV-4 follow-up issues filed on GitHub

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

STOP after PR creation. Do NOT call `gh pr merge` — Orchestrator reviews and merges on GitHub.

---

## Self-review pass

**Spec coverage:** every section in the design spec has a corresponding task:
- Spec §3 (Module shape) → Task 4
- Spec §4 (Field inventory) → Task 4 (fields + defaults)
- Spec §5 (Source resolution & warnings) → Tasks 4 (model_config), 5 (strip), 7 (warnings)
- Spec §6 (Test strategy) → Tasks 1–3
- Spec §7 (Security audit) → Task 9
- Spec §8 (Documentation deliverables) → Task 10
- Spec §9 (Follow-ups) → Task 11 steps 1–3
- Spec §10 (Memory) → Task 11 step 5
- Spec §11 (Commit plan) → explicit A/B/C pauses in Tasks 3, 8, 10, 11

**Placeholder scan:** no TBD/TODO/"fill in"/"appropriate error handling" in any step. The one deliberate conditional is the "fill in after implementation commit hash is known" for FEATURES.md Feature 3's Commit field, which has an explicit follow-up mechanism in Task 10 Step 6.

**Type consistency:** `Settings`, `get_settings`, `reload_settings`, `VALID_TOKEN`, `_isolated_env`, `secrets_dir` used consistently across conftest, tests, and module source.
