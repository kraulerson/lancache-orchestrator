"""F11 — ``game`` subcommands. ``show`` filters the list (no GET /games/{id})."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client
from orchestrator.cli.client import ApiError


@click.group()
def game() -> None:
    """Inspect and act on games."""


@game.command("list")
@click.option("--platform", type=click.Choice(["steam", "epic"]), default=None)
@click.option("--status", "status_", default=None)
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
@handles_api_errors
def game_list(ctx: click.Context, platform: str | None, status_: str | None, limit: int) -> None:
    """List games."""
    client = make_client(ctx)
    data = client.get("/api/v1/games", platform=platform, status=status_, limit=limit)
    rows = [
        [
            str(g["id"]),
            g["platform"],
            g["app_id"],
            (g.get("title") or "")[:40],
            output.status_label(g["status"]),
        ]
        for g in data["games"]
    ]
    click.echo(output.table(["ID", "PLATFORM", "APP_ID", "TITLE", "STATUS"], rows))


@game.command("show")
@click.argument("game_id", type=int)
@click.pass_context
@handles_api_errors
def game_show(ctx: click.Context, game_id: int) -> None:
    """Show one game (filters the list — no detail endpoint exists)."""
    client = make_client(ctx)
    data = client.get("/api/v1/games", limit=500)
    match = next((g for g in data["games"] if g["id"] == game_id), None)
    if match is None:
        raise ApiError(f"game {game_id} not found (in the first 500)")
    for key, value in match.items():
        rendered = output.status_label(value) if key == "status" else value
        click.echo(f"{key:18} {rendered}")


def _trigger(ctx: click.Context, game_id: int, path: str, name: str) -> None:
    client = make_client(ctx)
    resp = client.post(f"/api/v1/games/{game_id}/{path}")
    output.success(f"queued {name} for game {game_id} (job_id={resp['job_id']}).")


@game.command("prefill")
@click.argument("game_id", type=int)
@click.pass_context
@handles_api_errors
def game_prefill(ctx: click.Context, game_id: int) -> None:
    """Trigger a prefill."""
    _trigger(ctx, game_id, "prefill", "prefill")


@game.command("validate")
@click.argument("game_id", type=int)
@click.pass_context
@handles_api_errors
def game_validate(ctx: click.Context, game_id: int) -> None:
    """Trigger a validation."""
    _trigger(ctx, game_id, "validate", "validate")


@game.command("manifest")
@click.argument("game_id", type=int)
@click.pass_context
@handles_api_errors
def game_manifest(ctx: click.Context, game_id: int) -> None:
    """Trigger a manifest fetch."""
    _trigger(ctx, game_id, "manifest/fetch", "manifest_fetch")
