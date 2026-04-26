"""Shared fixtures for orchestrator.db tests.

#23 baseline: scrub ORCH_* env vars, inject dummy ORCH_TOKEN, clear
get_settings() cache before/after each test.

BL4 additions: pool fixtures (db_path, pool, mem_pool, populated_pool,
reset_singleton) for tests/db/test_pool*.py.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import os
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from orchestrator.core.settings import get_settings
from orchestrator.db import migrate

if TYPE_CHECKING:
    from pathlib import Path


VALID_TOKEN = "a" * 32


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    """Scrub ORCH_* env vars, inject a valid dummy ORCH_TOKEN, and clear the
    get_settings() cache before every tests/db/ test. Required because
    migrate.py + pool.py both call get_settings(), which refuses construction
    without orchestrator_token.
    """
    for key in list(os.environ):
        if key.startswith("ORCH_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ORCH_TOKEN", "a" * 32)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_path(tmp_path: Path) -> Path:
    """Fresh DB file per test with all migrations applied. ~50ms setup."""
    path = tmp_path / "pool_test.db"
    # run_migrations is sync; offload to thread to avoid blocking the loop
    await asyncio.to_thread(migrate.run_migrations, path)
    return path


@pytest_asyncio.fixture
async def pool(db_path: Path):
    """Standard tmp-file pool (4 readers). Realistic connection semantics."""
    from orchestrator.db.pool import Pool

    async with Pool.create(
        database_path=db_path,
        readers_count=4,
    ) as p:
        yield p


@pytest_asyncio.fixture
async def mem_pool():
    """Shared-cache :memory: pool (2 readers). For pure-API tests that don't
    care about file semantics. ~5ms setup. Schema is seeded directly via
    aiosqlite + 0001_initial.sql since :memory:cache=shared doesn't survive
    run_migrations() cleanly across separate connections.
    """
    from orchestrator.db.pool import Pool

    db_uri = "file::memory:?cache=shared&mode=memory&uri=true"
    async with Pool.create(
        database_path=db_uri,
        readers_count=2,
        skip_schema_verify=True,  # we'll seed manually
    ) as p:
        # Seed schema via the writer connection
        async with p.acquire_writer() as conn:
            schema_sql = (
                importlib.resources.files("orchestrator.db.migrations")
                .joinpath("0001_initial.sql")
                .read_text()
            )
            await conn.executescript(schema_sql)
            await conn.commit()
        yield p


@pytest_asyncio.fixture
async def populated_pool(pool):
    """tmp-file pool seeded with realistic test data:
      5 games, 3 manifests, 2 jobs, 4 cache_observations.
    For tests that need rows to query.
    """
    async with pool.write_transaction() as tx:
        # 5 games (3 steam, 2 epic)
        for _i, (platform, app_id, title) in enumerate(
            [
                ("steam", "10", "Counter-Strike"),
                ("steam", "440", "Team Fortress 2"),
                ("steam", "570", "Dota 2"),
                ("epic", "fortnite", "Fortnite"),
                ("epic", "rocketleague", "Rocket League"),
            ]
        ):
            await tx.execute(
                "INSERT INTO games (platform, app_id, title, owned, status) "
                "VALUES (?, ?, ?, 1, 'never_prefilled')",
                (platform, app_id, title),
            )
        # 3 manifests for the first 3 games
        for game_id in (1, 2, 3):
            await tx.execute(
                "INSERT INTO manifests (game_id, raw, fetched_at) "
                "VALUES (?, ?, '2026-04-25T00:00:00Z')",
                (game_id, b"manifest-stub-bytes"),
            )
        # 2 jobs
        for kind, state in [("prefill", "running"), ("library_sync", "succeeded")]:
            await tx.execute(
                "INSERT INTO jobs (kind, state, started_at) VALUES (?, ?, '2026-04-25T00:00:00Z')",
                (kind, state),
            )
        # 4 cache_observations
        for event in ["hit", "miss", "hit", "expired"]:
            await tx.execute(
                "INSERT INTO cache_observations (observed_at, event, cache_identifier, path) "
                "VALUES ('2026-04-25T00:00:00Z', ?, 'steam', '/example/path')",
                (event,),
            )
    return pool


@pytest.fixture
def reset_singleton(monkeypatch):
    """Reset the module-level _pool to None between tests that exercise
    init_pool() / get_pool() / close_pool()."""
    import orchestrator.db.pool as pool_mod

    monkeypatch.setattr(pool_mod, "_pool", None)
