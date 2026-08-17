"""Shared path-parameter types for the API routers (issue #263).

`game_id: int` was unbounded on every route that accepts one, so an id outside
SQLite's INTEGER range reached aiosqlite parameter binding and raised
`OverflowError`. `Pool.read_one` wraps only `aiosqlite.Error` and the routes
catch only `PoolError`, so nothing converted it: the request returned HTTP 500
with an unhandled traceback, tripping 5xx alerting on trivially-reachable
input.

Bounding at the validation layer rejects the value before it reaches the DB.
The status is 400, not 422: this app maps `RequestValidationError` to 400
globally (see `api/main.py`).

The lower bound is not cosmetic. SQLite's INTEGER range is *signed*, so the
negative end overflows exactly like the positive end; `ge=1` closes it, and it
matches the fact that rowids start at 1. The resulting contract change is
deliberate and approved: id 0 and negative ids answered 404 before and answer
400 now.

One alias, imported by every route that takes a game id, so the bound cannot
drift between the read path and the trigger paths.

Importers carry `# noqa: TC001`. Ruff sees an annotation-only use and offers to
move the import into a `TYPE_CHECKING` block, but FastAPI resolves route
annotations at runtime — under `from __future__ import annotations` the name
must exist in module globals or route registration raises `NameError`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

from orchestrator.api._query_helpers import INT64_MAX

# `le` is the largest value SQLite can bind as an INTEGER; `ge` is the lowest
# id the schema can actually issue.
GameIdPath = Annotated[int, Path(ge=1, le=INT64_MAX)]
