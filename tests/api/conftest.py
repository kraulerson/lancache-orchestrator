"""Shared fixtures for tests/api/.

Per spec §7.1: two app fixtures (unit_app no-lifespan; lifespan_app via
asgi_lifespan.LifespanManager) and three client fixtures (default,
loopback-simulated, external-IP-simulated for OQ2 testing).

Re-exports populated_pool from tests/db/conftest.py via direct import.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest_asyncio

# Re-use the pool fixtures from tests/db/conftest.py — these are
# discoverable by pytest as long as conftest.py at tests/ level is
# loaded, but explicit import for clarity.
from tests.db.conftest import (  # noqa: F401
    _isolated_env,
    db_path,
    mem_pool,
    pool,
    populated_pool,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI


@pytest_asyncio.fixture
async def unit_app(populated_pool):  # noqa: F811  pytest fixture inheritance via import
    """Fast unit-test app: no lifespan, deps overridden, app.state stubbed."""
    from orchestrator.api.dependencies import get_pool_dep
    from orchestrator.api.main import create_app

    app = create_app()
    app.dependency_overrides[get_pool_dep] = lambda: populated_pool
    app.state.boot_time = time.monotonic()
    app.state.git_sha = "test-sha-deadbeef"
    return app


@pytest_asyncio.fixture
async def lifespan_app(db_path: Path, monkeypatch) -> AsyncIterator[FastAPI]:  # noqa: F811
    """Integration-test app: real lifespan via asgi_lifespan."""
    from asgi_lifespan import LifespanManager

    from orchestrator.api.main import create_app

    monkeypatch.setenv("ORCH_DATABASE_PATH", str(db_path))
    app = create_app()
    async with LifespanManager(app):
        yield app


@pytest_asyncio.fixture
async def client(unit_app) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient hitting the unit_app via ASGITransport (no socket)."""
    transport = httpx.ASGITransport(app=unit_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def loopback_client(unit_app) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient that simulates a 127.0.0.1 origin (OQ2 positive-path test)."""
    transport = httpx.ASGITransport(app=unit_app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def external_client(unit_app) -> AsyncIterator[httpx.AsyncClient]:
    """AsyncClient that simulates a non-loopback origin (OQ2 negative-path test)."""
    transport = httpx.ASGITransport(app=unit_app, client=("192.168.1.100", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
