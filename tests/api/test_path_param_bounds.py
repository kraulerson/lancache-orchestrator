"""Cross-router guard: every `game_id` path parameter carries the #263 bound.

`_path_params.GameIdPath` bounded the four routes that existed when #263 was
filed, but nothing stops a *fifth* route from typing `game_id: int` again and
reintroducing the failure: Python ints are unbounded, so a value outside
SQLite's signed 64-bit INTEGER range reaches aiosqlite parameter binding and
raises OverflowError, which `Pool.read_one` (aiosqlite.Error only) and the
routes (PoolError only) both let escape as an unhandled HTTP 500.

The sweep reads the generated OpenAPI schema rather than route internals. That
is the contract clients actually see, and it survives FastAPI reshuffling its
internals — 0.137 stopped flattening included routers into `app.routes`, which
silently empties any sweep written against that attribute.
"""

from __future__ import annotations

from orchestrator.api._query_helpers import INT64_MAX

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})

# Routes known to take a game id when this guard was written. Asserted as a
# subset below so a discovery bug — or a router quietly dropped from
# create_app() — fails loudly instead of vacuously passing over an empty sweep.
_KNOWN_GAME_ID_ROUTES = {
    ("get", "/api/v1/games/{game_id}"),
    ("post", "/api/v1/games/{game_id}/prefill"),
    ("post", "/api/v1/games/{game_id}/validate"),
    ("post", "/api/v1/games/{game_id}/purge"),
}


def _game_id_params(app):
    """Yield (method, path, schema) for every `game_id` path param in the app."""
    for path, path_item in app.openapi()["paths"].items():
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS:
                continue
            for param in operation.get("parameters", []):
                if param.get("name") == "game_id" and param.get("in") == "path":
                    yield method, path, param["schema"]


async def test_sweep_finds_every_known_game_id_route(unit_app):
    """Without this, a sweep that discovers nothing would pass the bound check
    below by finding no violations — the worst kind of green."""
    found = {(method, path) for method, path, _ in _game_id_params(unit_app)}
    assert found >= _KNOWN_GAME_ID_ROUTES, (
        f"sweep missed known routes: {_KNOWN_GAME_ID_ROUTES - found}"
    )


async def test_every_game_id_path_param_is_bounded(unit_app):
    unbounded = sorted(
        f"{method.upper()} {path}"
        for method, path, schema in _game_id_params(unit_app)
        if schema.get("minimum") != 1 or schema.get("maximum") != INT64_MAX
    )
    assert not unbounded, (
        f"these routes take an unbounded game_id: {unbounded}. Annotate it "
        f"GameIdPath (api/routers/_path_params.py) — a bare `int` is unbounded "
        f"in Python and overflows SQLite's signed INTEGER at bind time, "
        f"returning HTTP 500 instead of a 400 (#263)."
    )
