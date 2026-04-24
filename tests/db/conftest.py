"""Shared fixtures for orchestrator.db tests.

Mirrors tests/core/conftest.py's pattern: scrub ORCH_* env vars and clear
the get_settings() lru_cache between tests so that a test setting
monkeypatch.setenv("ORCH_REQUIRE_LOCAL_FS", ...) sees a fresh Settings
instance on the next run_migrations() call (which reads from
get_settings() after the BL3 rewire — issue #23).
"""

from __future__ import annotations

import os

import pytest

from orchestrator.core.settings import get_settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    """Scrub ORCH_* env vars, inject a valid dummy ORCH_TOKEN, and
    clear the get_settings() cache before every tests/db/ test.

    The dummy token is required because migrate.py now reads
    require_local_fs via get_settings(), and Settings construction
    refuses to start without orchestrator_token. These tests exercise
    ID1 (migrations), not token validation, so a fixed 32-char token
    is injected to let get_settings() succeed.
    """
    for key in list(os.environ):
        if key.startswith("ORCH_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
