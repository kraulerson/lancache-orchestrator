"""Shared fixtures for tests/validator/.

Reuses the pool fixtures from tests/db/conftest.py (same pattern as
tests/jobs/conftest.py) so validator engine tests can seed a real DB.
"""

from __future__ import annotations

from tests.db.conftest import (  # noqa: F401
    _isolated_env,
    db_path,
    mem_pool,
    pool,
    populated_pool,
)
