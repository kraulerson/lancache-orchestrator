"""Tests for orchestrator.core.settings.

Covers:
  1. Required fields (orchestrator_token is the only required field)
  2. Defaults (all 15 optional fields match Bible-sourced values)
  3. Field validators (boundary and type rejections)
  4. Source precedence (init > env > .env > secrets > default)
  5. Secret loading (env path, file path, both missing)
  6. Redaction (raw token never appears in repr/model_dump forms)
  7. Warning emission (4 diagnostic warnings + 1 negative case)
  8. Singleton behavior (get_settings / reload_settings)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.core import logging as log_mod
from orchestrator.core.settings import Settings, get_settings, reload_settings
from tests.core.conftest import VALID_TOKEN


def _json_lines(captured_out: str) -> list[dict]:
    """Parse structlog JSON output captured via capsys — mirrors
    tests/core/test_logging.py helpers."""
    return [json.loads(line) for line in captured_out.strip().split("\n") if line.strip()]


# ----------------------------------------------------------------------
# 1. Required fields
# ----------------------------------------------------------------------


class TestRequiredFields:
    def test_missing_token_raises(self):
        # Token-related validation errors are re-raised as ValueError
        # (not pydantic's ValidationError) to scrub input echo. See
        # Settings.__init__ in settings.py.
        with pytest.raises(ValueError) as exc_info:
            Settings()
        msg = str(exc_info.value).lower()
        assert "orchestrator_token" in msg or "token" in msg

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

    @pytest.mark.parametrize(
        "field,expected",
        [
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
            ("pool_readers", 8),
            ("pool_busy_timeout_ms", 5000),
            ("db_cache_size_kib", 16384),
            ("db_mmap_size_bytes", 268_435_456),
            ("db_journal_size_limit_bytes", 67_108_864),
        ],
    )
    def test_default_value(self, settings, field, expected):
        assert getattr(settings, field) == expected


# ----------------------------------------------------------------------
# 3. Field validators
# ----------------------------------------------------------------------


class TestFieldValidators:
    def test_token_too_short_rejects(self):
        with pytest.raises(ValueError):
            Settings(orchestrator_token="a" * 31)

    def test_token_exactly_32_accepts(self):
        Settings(orchestrator_token="a" * 32)

    def test_token_with_whitespace_stripped_to_32(self):
        raw = "  " + "x" * 32 + "\n"
        s = Settings(orchestrator_token=raw)
        assert s.orchestrator_token.get_secret_value() == "x" * 32

    def test_token_with_whitespace_below_32_after_strip_rejects(self):
        with pytest.raises(ValueError):
            Settings(orchestrator_token="  " + "x" * 30 + "  ")

    def test_token_passed_as_secretstr_is_stripped(self):
        from pydantic import SecretStr

        raw = "  " + "y" * 32 + "\n"
        s = Settings(orchestrator_token=SecretStr(raw))
        assert s.orchestrator_token.get_secret_value() == "y" * 32

    def test_uat2_v5_token_with_null_byte_rejected(self):
        """V-5: Token containing NUL must be rejected. NUL would truncate
        log lines and could enable downstream parser confusion."""
        raw = "a" * 31 + "\x00"  # 32 chars but contains NUL
        with pytest.raises(ValueError):
            Settings(orchestrator_token=raw)

    def test_uat2_v5_token_with_crlf_rejected(self):
        """V-5: Token containing CR/LF must be rejected. Embedded line
        breaks could enable log-line injection or HTTP header smuggling
        if echoed back. Trailing whitespace is still stripped (existing
        behavior) — only embedded control chars in the body are rejected."""
        raw = "a" * 16 + "\r\n" + "a" * 14  # 32 chars, embedded CRLF
        with pytest.raises(ValueError):
            Settings(orchestrator_token=raw)

    def test_uat2_v5_token_with_tab_in_body_rejected(self):
        """V-5: Token with embedded tab is rejected. (Trailing tab/whitespace
        is stripped by _strip_token before length check.)"""
        raw = "a" * 16 + "\t" + "a" * 15  # 32 chars, embedded TAB
        with pytest.raises(ValueError):
            Settings(orchestrator_token=raw)

    def test_uat2_v5_clean_token_with_only_trailing_whitespace_still_works(self):
        """V-5 must not regress legitimate tokens — trailing whitespace
        is still stripped (Bible §7.3 contract)."""
        raw = "  " + "a" * 32 + "  \n"
        s = Settings(orchestrator_token=raw)
        assert s.orchestrator_token.get_secret_value() == "a" * 32

    def test_short_token_validation_error_does_not_echo_raw(self):
        """SEV-2 regression: pydantic's ValidationError.input_value
        echoes the raw rejected value unconditionally. On a startup
        failure during token rotation, that candidate token would
        land in logs. Settings.__init__ intercepts token-related
        ValidationErrors and re-raises as ValueError with a scrubbed
        message, closing the leak at the entry boundary."""
        raw = "NEVER_APPEAR_IN_LOGS_25CHAR"  # 27 chars — below 32 min
        with pytest.raises(ValueError) as exc_info:
            Settings(orchestrator_token=raw)
        err_str = str(exc_info.value)
        assert raw not in err_str, f"raw token leaked to ValueError: {err_str}"

    def test_settings_not_pickleable(self):
        """SEV-2 regression: pydantic.SecretStr's _secret_value attribute
        pickles the raw token. A future code path that pickles Settings
        (multiprocessing, Celery task args, on-disk cache) would write
        the cleartext to an attacker-readable location. We block this
        primitive by raising TypeError on pickling.
        """
        import pickle

        s = Settings(orchestrator_token=VALID_TOKEN)
        with pytest.raises(TypeError, match="not pickle-safe"):
            pickle.dumps(s)

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

    def test_pool_readers_zero_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, pool_readers=0)

    def test_pool_readers_33_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, pool_readers=33)

    def test_pool_busy_timeout_negative_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, pool_busy_timeout_ms=-1)

    def test_db_cache_size_below_min_rejects(self):
        with pytest.raises(ValidationError):
            Settings(orchestrator_token=VALID_TOKEN, db_cache_size_kib=1023)

    def test_db_journal_size_limit_below_min_rejects(self):
        with pytest.raises(ValidationError):
            Settings(
                orchestrator_token=VALID_TOKEN,
                db_journal_size_limit_bytes=1_048_575,
            )


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
        # Token-field errors re-raise as ValueError; see Settings.__init__.
        with pytest.raises(ValueError):
            Settings()


# ----------------------------------------------------------------------
# 6. Redaction
# ----------------------------------------------------------------------


REDACTION_TOKEN_SHAPES = [
    pytest.param("a" * 32, id="alphanumeric"),
    pytest.param("0123456789abcdef" * 4, id="hex"),
    pytest.param("Zm9vYmFyYmF6" + "=" * 20, id="base64-padding"),
    # NB: previously had `"x"*16 + "\n" + "y"*16` ("embedded-newline"). UAT-2
    # finding V-5 made embedded control chars an outright validation rejection,
    # so the redaction-of-newline-token shape is now unreachable. Coverage is
    # provided by the V-5 regression tests that assert rejection.
    pytest.param("p1+p2-mixed-symbols-padding-XYZ#", id="symbol-rich"),
    pytest.param("🔒secret-ünïcödé-token-padded-000", id="unicode"),
]


class TestRedaction:
    @pytest.mark.parametrize("raw", REDACTION_TOKEN_SHAPES)
    def test_raw_not_in_repr(self, raw):
        s = Settings(orchestrator_token=raw)
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
    """Capture warnings via capsys + JSON parse, matching ID3's
    tests/core/test_logging.py pattern. The structlog pipeline is
    configured per-test via log_mod.configure_logging().
    """

    def test_shadow_warning_fires(self, monkeypatch, secrets_dir, capsys):
        log_mod.configure_logging()
        (secrets_dir / "orchestrator_token").write_text("f" * 32)
        monkeypatch.setitem(Settings.model_config, "secrets_dir", str(secrets_dir))
        monkeypatch.setenv("ORCH_TOKEN", "e" * 32)
        Settings()
        events = [r.get("event") for r in _json_lines(capsys.readouterr().out)]
        assert "config.secret_shadowed_by_env" in events

    def test_non_loopback_host_warning_fires(self, capsys):
        log_mod.configure_logging()
        Settings(orchestrator_token=VALID_TOKEN, api_host="0.0.0.0")  # noqa: S104 — test confirms warning fires for non-loopback
        events = [r.get("event") for r in _json_lines(capsys.readouterr().out)]
        assert "config.api_bound_non_loopback" in events

    def test_wildcard_cors_warning_fires(self, capsys):
        log_mod.configure_logging()
        Settings(orchestrator_token=VALID_TOKEN, cors_origins=["*"])
        events = [r.get("event") for r in _json_lines(capsys.readouterr().out)]
        assert "config.cors_wildcard" in events

    def test_over_spike_f_concurrency_warning_fires(self, capsys):
        log_mod.configure_logging()
        Settings(orchestrator_token=VALID_TOKEN, chunk_concurrency=64)
        events = [r.get("event") for r in _json_lines(capsys.readouterr().out)]
        assert "config.chunk_concurrency_unvalidated" in events

    def test_no_warning_on_default_config(self, capsys):
        """Negative case: a valid-token-only Settings with defaults
        emits no config.* warnings."""
        log_mod.configure_logging()
        Settings(orchestrator_token=VALID_TOKEN)
        events = [r.get("event") for r in _json_lines(capsys.readouterr().out)]
        assert not any(e and e.startswith("config.") for e in events)

    def test_pool_readers_over_provisioned_warning_fires(self, capsys):
        """BL4: pool_readers > chunk_concurrency emits the warning."""
        log_mod.configure_logging()
        Settings(orchestrator_token=VALID_TOKEN, pool_readers=16, chunk_concurrency=8)
        events = [r.get("event") for r in _json_lines(capsys.readouterr().out)]
        assert "config.pool_readers_over_provisioned" in events

    def test_shadow_check_skipped_when_secrets_dir_is_none(self, monkeypatch, capsys):
        """Covers the defensive branch in _emit_config_warnings where
        model_config has no secrets_dir set. Must not raise or emit
        config.secret_shadowed_by_env even with ORCH_TOKEN in env."""
        log_mod.configure_logging()
        monkeypatch.setitem(Settings.model_config, "secrets_dir", None)
        monkeypatch.setenv("ORCH_TOKEN", "e" * 32)
        Settings()
        events = [r.get("event") for r in _json_lines(capsys.readouterr().out)]
        assert "config.secret_shadowed_by_env" not in events


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
