"""F11 — ``jobs`` list."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client

# Closed sets mirroring the jobs table CHECK constraint. Surfaced via click.Choice so
# a typo'd filter is rejected up front rather than returning a silent empty table
# (UAT-11 S11-E-04).
#
# This list was pinned to migration 0002 and never moved when 0009 added
# 'fetch_manifests' and 0014 added 'purge', so `jobs --kind purge` was rejected and a
# purge could not be audited from the CLI at all — half of F18's audit criterion
# (#296). tests/db/test_job_kinds_in_sync.py derives the expected set from the
# migrated schema, so the next kind added to a migration fails there loudly instead
# of silently going missing here.
_KINDS = [
    "prefill",
    "validate",
    "library_sync",
    "manifest_fetch",
    "fetch_manifests",
    "sweep",
    "auth_refresh",
    "purge",
]
_STATES = ["queued", "running", "succeeded", "failed", "cancelled"]


@click.command("jobs")
@click.option("--kind", type=click.Choice(_KINDS), default=None)
@click.option("--state", type=click.Choice(_STATES), default=None)
@click.option(
    "--limit", type=int, default=50, show_default=True, help="Max rows (server caps at 500)."
)
@click.pass_context
@handles_api_errors
def jobs(ctx: click.Context, kind: str | None, state: str | None, limit: int) -> None:
    """List jobs."""
    client = make_client(ctx)
    data = client.get("/api/v1/jobs", kind=kind, state=state, limit=limit)
    rows = [
        [
            str(j["id"]),
            j["kind"],
            j.get("platform") or "-",
            output.status_label(j["state"]),
            "" if j.get("progress") is None else f"{j['progress']:.0%}",
            (j.get("error") or "")[:30],
        ]
        for j in data["jobs"]
    ]
    click.echo(output.table(["ID", "KIND", "PLATFORM", "STATE", "PROGRESS", "ERROR"], rows))
