"""``cache`` subcommands — cache-maintenance operations (F11)."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client


@click.group()
def cache() -> None:
    """Cache-maintenance operations."""


@cache.command("fetch-manifests")
@click.pass_context
@handles_api_errors
def cache_fetch_manifests(ctx: click.Context) -> None:
    """Fetch manifests (no chunk bytes) for the cached library.

    Triggers the agent's DepotDownloader manifest-only run so the validator
    can cover apps that SteamPrefill skipped. Reuses the fetch_manifests
    in-flight dedup — at most one run at a time."""
    client = make_client(ctx)
    resp = client.post("/api/v1/fetch-manifests")
    job_id = resp["job_id"]
    if resp.get("queued"):
        output.success(f"queued manifest fetch (job_id={job_id}).")
    else:
        output.warn(f"a manifest fetch is already in flight (job_id={job_id}).")


@cache.command("validate-all")
@click.pass_context
@handles_api_errors
def cache_validate_all(ctx: click.Context) -> None:
    """Enqueue a full validation sweep over ALL games across platforms (backfill).

    Use after seeding the durable manifest archive so genuinely-cached games are
    re-checked and flip to up_to_date."""
    client = make_client(ctx)
    resp = client.post("/api/v1/sweep", json={"full": True})
    job_id = resp["job_id"]
    if resp.get("full"):
        output.success(f"queued full validation sweep (job_id={job_id}).")
    else:
        # The full=true request was deduped against an already-in-flight
        # NON-full sweep — the backfill is NOT running. Warn so the operator
        # isn't misled into thinking validate-all is underway.
        output.warn(
            f"a sweep is already in flight (job_id={job_id}) and it is NOT a full "
            "backfill — re-run `cache validate-all` after it completes."
        )
