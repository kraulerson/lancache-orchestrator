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


@pytest_asyncio.fixture
async def games_pool_100(populated_pool):  # noqa: F811
    """populated_pool seeded with 100 games for pagination tests.

    Adds 95 games to the 5 already in populated_pool. Mix of platforms
    (steam/epic), statuses (across the 8 enum values), and sizes for
    filter/sort coverage.
    """
    import json

    async with populated_pool.write_transaction() as tx:
        for i in range(6, 101):  # ids 6..100 (5 already exist)
            platform = "steam" if i % 2 == 0 else "epic"
            status = [
                "unknown",
                "not_downloaded",
                "up_to_date",
                "pending_update",
                "downloading",
                "validation_failed",
                "blocked",
                "failed",
            ][i % 8]
            await tx.execute(
                "INSERT INTO games "
                "(platform, app_id, title, owned, size_bytes, status, "
                "last_prefilled_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    platform,
                    f"app_{i:03d}",
                    f"Game {i:03d}",
                    i % 2,
                    i * 1_000_000_000,
                    status,
                    f"2026-05-{(i % 28) + 1:02d}T00:00:00Z" if i % 3 == 0 else None,
                    json.dumps({"depots": [i * 10, i * 10 + 1]}),
                ),
            )
    return populated_pool


@pytest_asyncio.fixture
async def jobs_pool_seeded(populated_pool):  # noqa: F811
    """populated_pool seeded with ~50 jobs across kinds/states/sources.

    Mix designed for BL8 filter+sort+pagination tests:
    - 5 kinds x multiple states (covers all kind/state enum values)
    - 4 sources represented
    - timestamps: queued has both NULL; running has started_at only;
      terminal states have both
    - progress: NULL for queued; partial for running; 1.0 for succeeded
    - error: populated only for failed jobs
    - payload: small dict on most; null on a few; one oversized (>64 KiB);
      one malformed JSON; one non-dict JSON (array)
    """
    import json as _json

    async with populated_pool.write_transaction() as tx:

        async def _ins(
            kind,
            state,
            *,
            game_id=None,
            platform=None,
            progress=None,
            source="scheduler",
            started_at=None,
            finished_at=None,
            error=None,
            payload=None,
        ):
            await tx.execute(
                "INSERT INTO jobs "
                "(kind, game_id, platform, state, progress, source, "
                " started_at, finished_at, error, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    game_id,
                    platform,
                    state,
                    progress,
                    source,
                    started_at,
                    finished_at,
                    error,
                    payload,
                ),
            )

        # Queued jobs (5)
        for i in range(5):
            await _ins(
                kind=["prefill", "validate", "library_sync", "auth_refresh", "sweep"][i],
                state="queued",
                game_id=(i + 1) if i < 3 else None,
                platform="steam" if i % 2 == 0 else "epic",
                payload=_json.dumps({"queued_at": f"2026-05-20T1{i}:00:00Z"}),
            )

        # Running jobs (5)
        for i in range(5):
            await _ins(
                kind=["prefill", "prefill", "validate", "library_sync", "sweep"][i],
                state="running",
                game_id=(i + 1) if i < 4 else None,
                platform="steam" if i % 2 == 0 else "epic",
                progress=0.1 + i * 0.2,
                source=["scheduler", "scheduler", "cli", "gameshelf", "api"][i],
                started_at=f"2026-05-20T1{i}:00:00Z",
                payload=_json.dumps({"depots": [100 + i, 101 + i]}),
            )

        # Succeeded jobs (20)
        for i in range(20):
            await _ins(
                kind=["prefill", "validate", "library_sync"][i % 3],
                state="succeeded",
                game_id=((i % 5) + 1),
                platform="steam" if i % 2 == 0 else "epic",
                progress=1.0,
                source=["scheduler", "scheduler", "scheduler", "cli"][i % 4],
                started_at=f"2026-05-{15 + (i % 5):02d}T08:00:00Z",
                finished_at=f"2026-05-{15 + (i % 5):02d}T09:00:00Z",
                payload=_json.dumps({"bytes": 1000000 * (i + 1)}),
            )

        # Failed jobs (10)
        for i in range(10):
            await _ins(
                kind=["prefill", "auth_refresh"][i % 2],
                state="failed",
                game_id=((i % 3) + 1),
                platform="steam",
                progress=0.5 + (i % 5) * 0.1,
                source="scheduler",
                started_at=f"2026-05-{10 + (i % 8):02d}T10:00:00Z",
                finished_at=f"2026-05-{10 + (i % 8):02d}T11:00:00Z",
                error=f"simulated failure #{i}: " + ("x" * 50),
                payload=_json.dumps({"attempt": i + 1}),
            )

        # Cancelled jobs (5)
        for i in range(5):
            await _ins(
                kind="sweep",
                state="cancelled",
                source="cli",
                started_at=f"2026-05-{5 + i:02d}T12:00:00Z",
                finished_at=f"2026-05-{5 + i:02d}T12:01:00Z",
                payload=_json.dumps({"reason": "operator_abort"}),
            )

        # One job with NULL payload
        await _ins(
            kind="sweep",
            state="succeeded",
            source="scheduler",
            started_at="2026-04-01T00:00:00Z",
            finished_at="2026-04-01T00:05:00Z",
        )

        # One job with oversized payload (>64 KiB)
        big = _json.dumps({"data": "x" * 70000})
        await _ins(
            kind="prefill",
            state="succeeded",
            game_id=1,
            platform="steam",
            progress=1.0,
            started_at="2026-04-02T00:00:00Z",
            finished_at="2026-04-02T01:00:00Z",
            payload=big,
        )

        # One job with malformed JSON payload
        await _ins(
            kind="validate",
            state="failed",
            game_id=2,
            platform="steam",
            started_at="2026-04-03T00:00:00Z",
            finished_at="2026-04-03T00:01:00Z",
            error="json corrupt",
            payload="{not valid json",
        )

        # One job with non-dict JSON payload (array)
        await _ins(
            kind="sweep",
            state="succeeded",
            source="scheduler",
            started_at="2026-04-04T00:00:00Z",
            finished_at="2026-04-04T00:05:00Z",
            payload=_json.dumps([1, 2, 3]),
        )

    return populated_pool


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
