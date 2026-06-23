"""F11 — ``auth`` subcommands. Credentials/codes are prompted (hidden) and sent
to the API; they are NEVER echoed or logged.

Steam no longer authenticates through the orchestrator — the host-side
SteamPrefill binary owns Steam auth (persistent, re-arch ③). Only Epic auth and
the cross-platform status view remain here."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client


@click.group()
def auth() -> None:
    """Authenticate Epic, or show auth status."""


@auth.command("epic")
@click.pass_context
@handles_api_errors
def auth_epic(ctx: click.Context) -> None:
    """Submit an Epic authorization code from legendary.gl/epiclogin."""
    client = make_client(ctx)
    code = click.prompt("Epic authorization code", hide_input=True)
    resp = client.post("/api/v1/platforms/epic/auth", json={"code": code})
    output.success(
        f"SUCCESS — epic authenticated ({resp.get('display_name')} / {resp.get('account_id')})."
    )


@auth.command("status")
@click.pass_context
@handles_api_errors
def auth_status(ctx: click.Context) -> None:
    """Show per-platform auth status."""
    client = make_client(ctx)
    data = client.get("/api/v1/platforms")
    rows = [
        [
            p["name"],
            output.status_label(p["auth_status"]),
            p.get("last_sync_at") or "-",
            (p.get("last_error") or "-")[:40],
        ]
        for p in data["platforms"]
    ]
    click.echo(output.table(["PLATFORM", "AUTH", "LAST_SYNC", "LAST_ERROR"], rows))
