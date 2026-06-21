"""Shared fixtures for tests/agent/.

Re-uses the autouse `_isolated_env` fixture from tests/db/conftest.py so that
every agent test runs with ORCH_* scrubbed, a valid dummy ORCH_TOKEN ("a"*32)
injected, and the get_settings() cache cleared. The agent's BearerAuthMiddleware
reads the global get_settings() for the expected token, so the dummy token must
match the one the tests present as a bearer.
"""

from __future__ import annotations

from tests.db.conftest import _isolated_env  # noqa: F401
