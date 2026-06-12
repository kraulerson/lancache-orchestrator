"""F11 — ``config show``: the effective settings, secrets redacted."""

from __future__ import annotations

import click

from orchestrator.core.settings import get_settings

# Redact by FIELD NAME, not just by type: SecretStr redacts itself, but a
# secret-bearing plain `str` (e.g. ``epic_client_secret``) would print raw.
# Any field whose name contains one of these tokens is masked.
_SECRET_NAME_MARKERS = ("token", "secret", "password")
_REDACTED = "**********"


def _is_secret_name(key: str) -> bool:
    low = key.lower()
    return any(marker in low for marker in _SECRET_NAME_MARKERS)


@click.group()
def config() -> None:
    """Inspect effective configuration."""


@config.command("show")
def config_show() -> None:
    """Print the effective settings (secrets redacted)."""
    # model_dump() keeps SecretStr fields as SecretStr objects (str(SecretStr) is
    # '**********'); plain-str secrets are redacted by name so they never print.
    data = get_settings().model_dump()
    for key in sorted(data):
        value = _REDACTED if _is_secret_name(key) else f"{data[key]!s}"
        click.echo(f"{key:34} {value}")
