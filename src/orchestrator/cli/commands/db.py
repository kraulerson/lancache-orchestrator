"""F11 — ``db`` local admin commands. These have no API endpoint by design
(don't expose schema/maintenance ops over HTTP); they run in-process against
``settings.database_path``."""

from __future__ import annotations

import asyncio

import aiosqlite
import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_db_errors
from orchestrator.core.settings import get_settings
from orchestrator.db.migrate import run_migrations


@click.group()
def db() -> None:
    """Local database admin (migrate, vacuum)."""


@db.command("migrate")
@handles_db_errors
def db_migrate() -> None:
    """Apply all pending migrations to the configured database."""
    db_path = str(get_settings().database_path)
    run_migrations(db_path)
    output.success(f"migrations applied — schema current ({db_path}).")


async def _vacuum(db_path: str) -> None:
    # isolation_level=None (autocommit) — VACUUM cannot run inside a transaction.
    async with aiosqlite.connect(db_path, isolation_level=None) as conn:
        await conn.execute("VACUUM")


@db.command("vacuum")
@handles_db_errors
def db_vacuum() -> None:
    """Reclaim free pages with VACUUM."""
    db_path = str(get_settings().database_path)
    asyncio.run(_vacuum(db_path))
    output.success(f"vacuum complete ({db_path}).")
