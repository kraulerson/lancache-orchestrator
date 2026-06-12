"""F11 — ``auth`` subcommands. Credentials/codes are prompted (hidden) and sent
to the API; they are NEVER echoed or logged."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client

_VALVE_WARNING = (
    "Valve may email you about a 'new device' sign-in — this is expected, not a "
    "compromise. Do not change your password in response (it would expire this session)."
)


@click.group()
def auth() -> None:
    """Authenticate Steam / Epic, or show auth status."""


@auth.command("steam")
@click.pass_context
@handles_api_errors
def auth_steam(ctx: click.Context) -> None:
    """Interactive Steam login (username + password + Steam Guard)."""
    client = make_client(ctx)
    username = click.prompt("Steam username")
    password = click.prompt("Steam password", hide_input=True)
    resp = client.post(
        "/api/v1/platforms/steam/auth", json={"username": username, "password": password}
    )
    if resp and "challenge_id" in resp:
        ctype = resp.get("challenge_type", "code")
        code = click.prompt(f"Steam Guard code ({ctype})", hide_input=True)
        resp = client.post(
            f"/api/v1/platforms/steam/auth/{resp['challenge_id']}", json={"code": code}
        )
    output.success(f"SUCCESS — steam authenticated (steam_id={resp.get('steam_id')}).")
    output.warn(_VALVE_WARNING)


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
