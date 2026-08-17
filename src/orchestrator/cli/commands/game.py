"""F11 — ``game`` subcommands. Id lookups use GET /games/{game_id}."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import click

from orchestrator.cli import output
from orchestrator.cli.base import handles_api_errors, make_client
from orchestrator.cli.client import ApiError, OrchClient

# games.status CHECK set — surfaced via click.Choice so a typo'd --status is
# rejected up front instead of silently returning an empty table (UAT-11 S11-E-04).
_STATUSES = [
    "unknown",
    "not_downloaded",
    "up_to_date",
    "pending_update",
    "downloading",
    "validation_failed",
    "blocked",
    "failed",
]


def _positive_int(ctx: click.Context, param: click.Parameter, value: int) -> int:
    """Reject a non-positive game id with an actionable message (UAT-11 S11-E-05)."""
    if value < 1:
        raise click.BadParameter("game id must be a positive integer")
    return value


@click.group()
def game() -> None:
    """Inspect and act on games."""


def _non_negative_int(ctx: click.Context, param: click.Parameter, value: int) -> int:
    """Reject a negative --offset locally; the server would 400 it anyway (#264)."""
    if value < 0:
        raise click.BadParameter("offset must be zero or greater")
    return value


@game.command("list")
@click.option("--platform", type=click.Choice(["steam", "epic"]), default=None)
@click.option("--status", "status_", type=click.Choice(_STATUSES), default=None)
@click.option("--title", default=None, help="Case-insensitive substring of the title.")
@click.option(
    "--limit", type=int, default=50, show_default=True, help="Max rows (server caps at 500)."
)
@click.option(
    "--offset",
    type=int,
    default=0,
    callback=_non_negative_int,
    help="Skip this many rows — page through a result set larger than --limit.",
)
@click.pass_context
@handles_api_errors
def game_list(
    ctx: click.Context,
    platform: str | None,
    status_: str | None,
    title: str | None,
    limit: int,
    offset: int,
) -> None:
    """List games."""
    client = make_client(ctx)
    data = client.get(
        "/api/v1/games",
        platform=platform,
        status=status_,
        # Substring search, so the server's `contains` op — a bare `title=` is
        # an exact match, which the games allow-list rejects with a 400 (#264).
        title_contains=title,
        limit=limit,
        # Send offset only when paging; 0 is the server default anyway.
        offset=offset or None,
    )
    games = data["games"]
    rows = [
        [
            str(g["id"]),
            g["platform"],
            g["app_id"],
            (g.get("title") or "")[:40],
            output.status_label(g["status"]),
            "yes" if g.get("blocked") else "-",
        ]
        for g in games
    ]
    click.echo(output.table(["ID", "PLATFORM", "APP_ID", "TITLE", "STATUS", "BLOCKED"], rows))

    # #264: without this footer a capped table is indistinguishable from a
    # complete one, so an operator reads 500 rows as the whole library — and
    # since `game list` is the only way to DISCOVER an id, ids past the cap
    # were unreachable. `.get` throughout: a response without meta must still
    # render its table rather than raise.
    meta = data.get("meta") or {}
    if meta.get("has_more"):
        shown = meta.get("offset", offset) + len(games)
        click.echo(
            f"\nshowing {shown} of {meta.get('total', '?')} "
            f"— use --offset {shown} for the next page"
        )


def _fetch_game(client: OrchClient, game_id: int) -> dict[str, Any]:
    """Return the game row for ``game_id`` via ``GET /api/v1/games/{game_id}``.

    Resolving an id must not go through the list endpoint: the server caps
    ``/games`` at 500 rows, so a list-scan cannot see most of the library and
    silently fails for higher ids (#260).

    A 404 is deliberately NOT re-worded. The server already answers a missing
    game with ``game not found``, so the client's own ``HTTP 404: game not
    found`` is clear; rewriting it required classifying the status, which
    conflated a missing game with a missing *route* (a wrong ``--url``) and
    reproduced the very misdiagnosis #260 exists to remove.
    """
    data = client.get(f"/api/v1/games/{game_id}")
    game: dict[str, Any] = data["game"]
    return game


@game.command("show")
@click.argument("game_id", type=int, callback=_positive_int)
@click.pass_context
@handles_api_errors
def game_show(ctx: click.Context, game_id: int) -> None:
    """Show one game."""
    match = _fetch_game(make_client(ctx), game_id)
    # Render every field before writing any of it: a malformed field must not
    # leave a half-written record on stdout that a redirect would capture as a
    # valid (just short) game (#261).
    lines = [
        f"{key:18} {output.status_label(value) if key == 'status' else value}"
        for key, value in match.items()
    ]
    click.echo("\n".join(lines))


def _trigger(
    ctx: click.Context,
    game_id: int,
    path: str,
    name: str,
    params: dict[str, str] | None = None,
) -> None:
    client = make_client(ctx)
    resp = client.post(f"/api/v1/games/{game_id}/{path}", params=params)
    output.success(f"queued {name} for game {game_id} (job_id={resp['job_id']}).")


@game.command("prefill")
@click.argument("game_id", type=int, callback=_positive_int)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Re-request every chunk (SteamPrefill --force) to refill an evicted/partial game "
    "that a normal prefill would skip as already complete.",
)
@click.pass_context
@handles_api_errors
def game_prefill(ctx: click.Context, game_id: int, force: bool) -> None:
    """Trigger a prefill (use --force to refill an already-'complete' partial game)."""
    _trigger(ctx, game_id, "prefill", "prefill", params={"force": "true"} if force else None)


@game.command("validate")
@click.argument("game_id", type=int, callback=_positive_int)
@click.pass_context
@handles_api_errors
def game_validate(ctx: click.Context, game_id: int) -> None:
    """Trigger a validation."""
    _trigger(ctx, game_id, "validate", "validate")


@game.command("purge")
@click.argument("game_id", type=int, callback=_positive_int)
@click.pass_context
@handles_api_errors
def game_purge(ctx: click.Context, game_id: int) -> None:
    """Delete a game's cached chunks, then flag it for re-prefill (F18).

    Reversible: the game is re-downloaded from the CDN on the next prefill.
    """
    _trigger(ctx, game_id, "purge", "purge")


def _resolve_app(ctx: click.Context, game_id: int) -> tuple[OrchClient, str, str]:
    """Return (client, platform, app_id) for a known game id, or raise ApiError.

    Needed because the block-list is keyed by (platform, app_id), not game id."""
    client = make_client(ctx)
    match = _fetch_game(client, game_id)
    platform, app_id = match.get("platform"), match.get("app_id")
    # Validate here rather than leaning on what a downstream call happens to
    # reject: `unblock` only caught a bad app_id because quote() raises on
    # non-str, and nothing at all caught a bad platform — so a null field
    # reached the wire as the literal "None" and reported success (#261).
    if not isinstance(platform, str) or not platform:
        raise ApiError(f"game {game_id}: API response has no usable platform")
    if not isinstance(app_id, str) or not app_id:
        raise ApiError(f"game {game_id}: API response has no usable app_id")
    return client, platform, app_id


@game.command("block")
@click.argument("game_id", type=int, callback=_positive_int)
@click.option("--reason", default=None, help="Optional note (<=500 chars).")
@click.pass_context
@handles_api_errors
def game_block(ctx: click.Context, game_id: int, reason: str | None) -> None:
    """Exclude a game from scheduled prefill."""
    client, platform, app_id = _resolve_app(ctx, game_id)
    client.post(
        "/api/v1/block-list",
        json={"platform": platform, "app_id": app_id, "reason": reason, "source": "cli"},
    )
    output.success(f"blocked game {game_id} ({platform}:{app_id}) from scheduled prefill.")


@game.command("unblock")
@click.argument("game_id", type=int, callback=_positive_int)
@click.pass_context
@handles_api_errors
def game_unblock(ctx: click.Context, game_id: int) -> None:
    """Remove a game from the block list (idempotent)."""
    client, platform, app_id = _resolve_app(ctx, game_id)
    # Encode BOTH segments. An Epic appName can contain '/', and a '/'/'?'/'#'
    # in either field would otherwise split the path and re-target the DELETE at
    # a different block-list row while still reporting success (#261).
    resp = client.delete(f"/api/v1/block-list/{quote(platform, safe='')}/{quote(app_id, safe='')}")
    if (resp or {}).get("removed"):
        output.success(f"unblocked game {game_id} ({platform}:{app_id}).")
    else:
        output.success(f"game {game_id} ({platform}:{app_id}) was not blocked.")
