"""F11 — shared CLI runtime: client factory + error→exit-code decorator.

Lives apart from ``main.py`` so command modules import from here and ``main``
imports the commands — a one-way graph, no import cycle.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, cast

import aiosqlite  # re-exports sqlite3's exception classes (aiosqlite.Error is sqlite3.Error)
import click

from orchestrator.cli import client as _client
from orchestrator.db.migrate import MigrationError


def make_client(ctx: click.Context) -> _client.OrchClient:
    """Build an OrchClient from the cli context.

    Looks up ``OrchClient`` on the module (not a bound import) so tests can patch
    ``orchestrator.cli.client.OrchClient`` with a transport-bearing subclass.
    """
    return _client.OrchClient(base_url=ctx.obj["url"], token=ctx.obj["token"])


def handles_api_errors[F: Callable[..., Any]](fn: F) -> F:
    """Print the error + exit with the ``OrchClientError`` exit code.

    ``CliRunner`` reports ``SystemExit.code`` as the result ``exit_code``; the
    real entry point lets the ``SystemExit`` propagate to the process.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _client.OrchClientError as e:
            click.echo(f"✗ {e}", err=True)
            raise SystemExit(e.exit_code) from e
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            # Backstop: a 2xx body that omits/changes an expected field would
            # otherwise escape as a raw traceback. Surface it as a clean exit 1.
            click.echo(f"✗ unexpected response from orchestrator API ({e!r})", err=True)
            raise SystemExit(1) from e

    return cast("F", wrapper)


def handles_db_errors[F: Callable[..., Any]](fn: F) -> F:
    """For the in-process ``db`` commands: turn a local DB failure into a clean
    exit 1 + ``✗`` message instead of a raw traceback.

    These commands don't hit the API, so ``handles_api_errors`` doesn't apply —
    and ``MigrationError``/``sqlite3.Error``/``OSError`` aren't in its backstop
    tuple. This decorator honours the same F11 clean-error contract for them.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (MigrationError, aiosqlite.Error, OSError) as e:
            click.echo(f"✗ {e}", err=True)
            raise SystemExit(1) from e

    return cast("F", wrapper)
