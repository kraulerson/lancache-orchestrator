"""F11 — orchestrator-cli root. Hits the local REST API with the bearer token.

Exit codes (Manifesto F11): API unreachable -> 2, auth failure -> 3, other -> 1.
The per-command ``handles_api_errors`` decorator raises ``SystemExit`` with the
right code; ``CliRunner`` reports it, and the real entry lets it reach the shell.
"""

from __future__ import annotations

import os

import click

from orchestrator.cli.commands import auth, config, db, game, jobs, library, status

_DEFAULT_URL = "http://127.0.0.1:8765"


@click.group()
@click.option(
    "--url",
    envvar="ORCH_API_URL",
    default=_DEFAULT_URL,
    show_default=True,
    help="Base URL of the orchestrator API.",
)
@click.pass_context
def cli(ctx: click.Context, url: str) -> None:
    """Operator CLI for the lancache orchestrator."""
    ctx.ensure_object(dict)
    ctx.obj["url"] = url
    ctx.obj["token"] = os.environ.get("ORCH_TOKEN", "")


cli.add_command(auth.auth)
cli.add_command(library.library)
cli.add_command(status.status)
cli.add_command(game.game)
cli.add_command(jobs.jobs)
cli.add_command(db.db)
cli.add_command(config.config)


def main() -> None:
    """Console-script entry point (`orchestrator-cli`)."""
    cli()
