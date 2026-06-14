"""F11 — ``config show``: the effective settings, secrets redacted."""

from __future__ import annotations

import click

from orchestrator.cli.base import handles_local_errors
from orchestrator.core.settings import get_settings

# Redact by FIELD NAME, not just by type: SecretStr redacts itself, but a
# secret-bearing plain `str` (e.g. ``epic_client_secret``) would print raw. A
# field is masked if its name contains a secret marker AND is not an endpoint /
# path / id field — so ``epic_token_url`` (an OAuth endpoint, useful for
# debugging) is NOT over-redacted by the bare "token" substring (UAT-11 S11-E-06).
_SECRET_NAME_MARKERS = ("token", "secret", "password")
_NON_SECRET_SUFFIXES = ("_url", "_path", "_template", "_dir", "_id")
_REDACTED = "**********"


def _is_secret_name(key: str) -> bool:
    low = key.lower()
    if low.endswith(_NON_SECRET_SUFFIXES):
        return False
    return any(marker in low for marker in _SECRET_NAME_MARKERS)


@click.group()
def config() -> None:
    """Inspect effective configuration."""


@config.command("show")
@handles_local_errors
def config_show() -> None:
    """Print the effective settings (secrets redacted)."""
    # model_dump() keeps SecretStr fields as SecretStr objects (str(SecretStr) is
    # '**********'); plain-str secrets are redacted by name so they never print.
    data = get_settings().model_dump()
    # Width = the longest key, so long keys don't crush the value column (S11-E-10).
    width = max((len(k) for k in data), default=0)
    for key in sorted(data):
        value = _REDACTED if _is_secret_name(key) else f"{data[key]!s}"
        click.echo(f"{key:{width}} {value}")
