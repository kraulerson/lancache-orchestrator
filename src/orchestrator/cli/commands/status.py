"""F11 — ``status``: a colorblind-safe one-shot health + auth summary."""

from __future__ import annotations

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client

_HEALTH_FIELDS = [
    ("scheduler_running", "SCHEDULER"),
    ("lancache_reachable", "LANCACHE"),
    ("cache_volume_mounted", "CACHE_MOUNT"),
    ("validator_healthy", "VALIDATOR"),
]


@click.command("status")
@click.pass_context
@handles_api_errors
def status(ctx: click.Context) -> None:
    """Show overall orchestrator health + platform auth."""
    client = make_client(ctx)
    # /health returns 503-with-body when degraded; get_health() returns that body
    # so the summary still renders (a degraded health is what `status` is FOR).
    health = client.get_health()
    click.echo(f"orchestrator {health.get('version')} (git {health.get('git_sha')})")
    rows = [
        [label, output.status_label("ok" if health.get(key) else "error")]
        for key, label in _HEALTH_FIELDS
    ]
    plats = client.get("/api/v1/platforms")["platforms"]
    rows += [[f"AUTH:{p['name'].upper()}", output.status_label(p["auth_status"])] for p in plats]
    click.echo(output.table(["COMPONENT", "STATE"], rows))
