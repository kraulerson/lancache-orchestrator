"""LOG-1 (review 2026-06-23): the production entrypoints must actually install
the project's structlog chain (configure_logging). It was defined but never
called outside tests, so prod ran structlog's default ConsoleRenderer — the JSON
contract and the secret-redaction processor were both silently absent.

These tests assert the wiring: after the API and agent process entrypoints run,
structlog is configured and credential-shaped kwargs are redacted.
"""

from __future__ import annotations

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_structlog():
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def test_create_app_configures_structlog(monkeypatch):
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    from orchestrator.api.main import create_app
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    assert structlog.is_configured() is False  # precondition
    create_app()
    assert structlog.is_configured() is True


def test_create_app_logging_redacts_secrets(monkeypatch, capsys):
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    from orchestrator.api.main import create_app
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    create_app()
    # A unique logger name avoids cache_logger_on_first_use returning a logger
    # bound under an earlier (default) config.
    structlog.get_logger("log1.test").warning("evt", password="hunter2")  # noqa: S106  # redaction probe
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "<redacted>" in out


def test_agent_main_configures_structlog(monkeypatch):
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    import orchestrator.agent.__main__ as agent_main
    from orchestrator.core.settings import get_settings

    get_settings.cache_clear()
    ran = {"uvicorn": False}

    def _fake_run(app, **kw):
        ran["uvicorn"] = True

    monkeypatch.setattr(agent_main.uvicorn, "run", _fake_run)
    assert structlog.is_configured() is False  # precondition
    agent_main.main()
    assert structlog.is_configured() is True
    assert ran["uvicorn"] is True
