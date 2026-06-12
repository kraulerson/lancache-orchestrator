"""F11 — ``library`` subcommands."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client


@click.group()
def library() -> None:
    """Library sync operations."""


@library.command("sync")
@click.option(
    "--platform", type=click.Choice(["steam", "epic"]), default="steam", show_default=True
)
@click.pass_context
@handles_api_errors
def library_sync(ctx: click.Context, platform: str) -> None:
    """Trigger a library sync for a platform."""
    client = make_client(ctx)
    resp = client.post(f"/api/v1/platforms/{platform}/library/sync")
    output.success(f"queued library_sync for {platform} (job_id={resp['job_id']}).")
