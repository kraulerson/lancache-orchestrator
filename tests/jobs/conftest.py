"""Shared fixtures for tests/jobs/.

Reuses the `pool` fixture from tests/db/conftest.py via direct import.
Adds a `jobs_handler_clean_registry` autouse fixture so each test gets
the builtin HANDLERS registry restored after running — tests that
re-register stubs don't leak across the suite, and a
`clear_breaker_notice` autouse fixture that does the same for the
circuit breaker's module-level notification dedupe.
"""

from __future__ import annotations

import pytest

# Re-use the pool fixtures from tests/db/conftest.py — these are
# discoverable as long as conftest.py at tests/ level is loaded, but
# explicit import for clarity.
from tests.db.conftest import (  # noqa: F401
    _isolated_env,
    db_path,
    mem_pool,
    pool,
    populated_pool,
)


@pytest.fixture(autouse=True)
def jobs_handler_clean_registry():
    """Snapshot the HANDLERS dict before each test and restore after.

    Tests that call `clear()` + `register(stub)` mutate the module-level
    registry; without this fixture those mutations leak.
    """
    from orchestrator.jobs.handlers import HANDLERS

    snapshot = dict(HANDLERS)
    try:
        yield
    finally:
        HANDLERS.clear()
        HANDLERS.update(snapshot)


@pytest.fixture(autouse=True)
def clear_breaker_notice():
    """Reset `measurement`'s notify-once dedupe either side of every test.

    The dedupe is module-level state, so it outlives a test: a test that trips
    the breaker suppresses the next test's notification, and the pair passes or
    fails depending on collection order. It lives here rather than beside the
    breaker tests because any test under tests/jobs/ can reach
    `record_measurement()` through a handler.
    """
    from orchestrator.jobs import measurement

    measurement.reset_breaker_notice()
    yield
    measurement.reset_breaker_notice()
