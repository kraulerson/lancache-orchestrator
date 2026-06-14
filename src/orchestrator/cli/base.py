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


def handles_local_errors[F: Callable[..., Any]](fn: F) -> F:
    """For the in-process ``db``/``config`` commands: turn a local failure into a
    clean exit 1 + ``✗`` message instead of a raw traceback.

    These commands don't hit the API, so ``handles_api_errors`` doesn't apply.
    They call ``get_settings()`` first — which raises pydantic ``ValidationError``
    on a malformed ``ORCH_*`` env var, OR a scrubbed plain ``ValueError`` when the
    required ``ORCH_TOKEN`` is missing (``Settings.__init__`` re-raises it as a
    ``ValueError`` to keep the secret out of the message) — and then run
    migrations / open SQLite. None of these are in ``handles_api_errors``'s
    backstop tuple. This decorator honours the same F11 clean-error contract for
    them. ``ValueError`` covers the missing-token case (UAT-11 S11-E-01).
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (ValueError, MigrationError, aiosqlite.Error, OSError) as e:
            # ValidationError is a subclass of ValueError in pydantic v2, so
            # ValueError covers both the malformed-value and missing-token paths.
            click.echo(f"✗ {e}", err=True)
            raise SystemExit(1) from e

    return cast("F", wrapper)
