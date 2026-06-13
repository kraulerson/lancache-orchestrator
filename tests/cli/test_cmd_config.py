"""F11: config show — effective settings, secrets redacted."""

from __future__ import annotations

from click.testing import CliRunner

from orchestrator.cli.main import cli

# Built at runtime so the raw value never appears as a source literal (gitleaks).
_SECRET = "s3cr3t" + "-token-" + ("9" * 26)


def test_config_show_redacts_token(monkeypatch):
    monkeypatch.setenv("ORCH_TOKEN", _SECRET)
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    r = CliRunner().invoke(cli, ["config", "show"])
    get_settings.cache_clear()
    assert r.exit_code == 0, r.output
    assert "orchestrator_token" in r.output
    assert _SECRET not in r.output  # redacted to **********
    assert "database_path" in r.output


def test_config_show_malformed_env_exits_1_cleanly(monkeypatch):
    """A malformed ORCH_* env var makes get_settings() raise a pydantic
    ValidationError. The in-process command must surface a clean exit 1, not a
    raw multi-line traceback (audit 2026-06-09)."""
    monkeypatch.setenv("ORCH_TOKEN", _SECRET)
    monkeypatch.setenv("ORCH_API_PORT", "notaport")  # int field → ValidationError
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    r = CliRunner().invoke(cli, ["config", "show"])
    get_settings.cache_clear()
    assert r.exit_code == 1
    assert isinstance(r.exception, SystemExit)  # handled, not a raw ValidationError
    assert "✗" in r.stderr


def test_config_show_redacts_secret_named_fields(monkeypatch):
    """Any field whose name signals a secret (token/secret/password) must be
    redacted by NAME — not only SecretStr-typed fields. `epic_client_secret` is
    a plain `str`, so a type-only redaction would print its raw value."""
    monkeypatch.setenv("ORCH_TOKEN", _SECRET)
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    get_settings.cache_clear()
    raw_secret = settings.epic_client_secret
    assert raw_secret  # there is a value to leak

    r = CliRunner().invoke(cli, ["config", "show"])
    get_settings.cache_clear()
    assert r.exit_code == 0, r.output
    assert "epic_client_secret" in r.output
    assert raw_secret not in r.output  # redacted, not printed raw
