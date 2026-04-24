"""Shared fixtures for orchestrator.core tests.

Provides environment isolation so tests never inherit host-developer
ORCH_* env vars or a project-root .env file. Also resets structlog's
pipeline around every test to match ID3's (test_logging.py) pattern so
TestWarnings capture is deterministic. Every test starts from a clean
slate; individual tests opt in to specific values via monkeypatch.setenv
or explicit Settings(...) kwargs.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
import structlog

from orchestrator.core.settings import get_settings

if TYPE_CHECKING:
    from pathlib import Path

VALID_TOKEN = "a" * 32  # 32-character minimum for ORCH_TOKEN


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Scrub ORCH_* env vars, chdir to tmp_path (blocks host .env
    discovery), reset structlog, and clear the get_settings() cache.
    Runs before every test in tests/core/.
    """
    for key in list(os.environ):
        if key.startswith("ORCH_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    """Returns a freshly-created directory suitable for use as a
    monkeypatch.setitem(Settings.model_config, "secrets_dir", ...) target.
    """
    directory = tmp_path / "run_secrets"
    directory.mkdir()
    return directory
