"""``selection`` subcommands — Steam prefill-selection review (#229)."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client


@click.group()
def selection() -> None:
    """Steam prefill-selection maintenance."""


@selection.command("classify")
@click.pass_context
@handles_api_errors
def selection_classify(ctx: click.Context) -> None:
    """Flag prefill-selection apps that look like non-games.

    Lists soundtracks, tools/SDKs, dedicated servers, demos, and videos found in
    the store-info cache — CANDIDATES to remove from selectedAppsToPrefill.json.
    Nothing is changed; you decide what to drop. (A genuine utility Steam types
    as a game, e.g. Lossless Scaling, won't be flagged — that stays your call.)
    """
    client = make_client(ctx)
    resp = client.get("/api/v1/selection/candidates")
    candidates = resp.get("candidates", [])
    scanned = resp.get("total_scanned", 0)
    if not candidates:
        output.success(f"No exclusion candidates among {scanned} classified app(s).")
        return
    output.warn(f"{len(candidates)} exclusion candidate(s) of {scanned} classified app(s):")
    click.echo(
        output.table(
            ["app_id", "name", "type", "reason"],
            [[c["app_id"], c["name"], c["app_type"], c["reason"]] for c in candidates],
        )
    )
    click.echo("")
    click.echo(
        "Remove any you don't want cached from selectedAppsToPrefill.json. Nothing was changed."
    )
