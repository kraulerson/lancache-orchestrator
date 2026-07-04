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


def _split_app(spec: str) -> tuple[str, str]:
    """Parse a 'platform/app_id' spec (e.g. steam/440)."""
    platform, _, app_id = spec.partition("/")
    if not platform or not app_id:
        raise click.BadParameter("expected platform/app_id, e.g. steam/440")
    return platform, app_id


@selection.command("exclusions")
@click.pass_context
@handles_api_errors
def selection_exclusions(ctx: click.Context) -> None:
    """List prefill exclusions/allows (the auto-classify-block overrides)."""
    client = make_client(ctx)
    resp = client.get("/api/v1/prefill-exclusions")
    rows = resp.get("exclusions", [])
    if not rows:
        output.success("No prefill exclusions.")
        return
    click.echo(
        output.table(
            ["platform", "app_id", "mode", "source", "reason"],
            [
                [r["platform"], r["app_id"], r["mode"], r["source"], r.get("reason") or ""]
                for r in rows
            ],
        )
    )


@selection.command("allow")
@click.argument("spec")
@click.pass_context
@handles_api_errors
def selection_allow(ctx: click.Context, spec: str) -> None:
    """Force-ALLOW prefill for platform/app_id (sticky — the auto step won't re-exclude it)."""
    platform, app_id = _split_app(spec)
    client = make_client(ctx)
    client.post(f"/api/v1/prefill-exclusions/{platform}/{app_id}", json={"mode": "allow"})
    output.success(f"{platform}/{app_id} set to ALLOW (will be prefilled; never auto-excluded).")


@selection.command("exclude")
@click.argument("spec")
@click.pass_context
@handles_api_errors
def selection_exclude(ctx: click.Context, spec: str) -> None:
    """Manually EXCLUDE platform/app_id from scheduled prefill."""
    platform, app_id = _split_app(spec)
    client = make_client(ctx)
    client.post(f"/api/v1/prefill-exclusions/{platform}/{app_id}", json={"mode": "exclude"})
    output.success(f"{platform}/{app_id} set to EXCLUDE (skipped by scheduled prefill).")


@selection.command("unset")
@click.argument("spec")
@click.pass_context
@handles_api_errors
def selection_unset(ctx: click.Context, spec: str) -> None:
    """Clear any prefill-exclusion override for platform/app_id."""
    platform, app_id = _split_app(spec)
    client = make_client(ctx)
    client.delete(f"/api/v1/prefill-exclusions/{platform}/{app_id}")
    output.success(f"{platform}/{app_id} override cleared.")
