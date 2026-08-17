"""F11: game subcommands."""

from __future__ import annotations

import httpx

_GAMES = {
    "games": [
        {"id": 1, "platform": "steam", "app_id": "730", "title": "CS2", "status": "up_to_date"},
        {"id": 2, "platform": "epic", "app_id": "abc", "title": "Turaco", "status": "blocked"},
    ],
    "meta": {"total": 2},
}


def test_game_list_table(mock):
    r = mock(["game", "list"], lambda req: httpx.Response(200, json=_GAMES))
    assert r.exit_code == 0
    assert "CS2" in r.output and "UP_TO_DATE" in r.output.upper()


def test_game_list_sends_limit_not_per_page(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        # The real read endpoints use `limit`/`offset`; `per_page` => 400 (UAT-10).
        assert "limit" in dict(req.url.params)
        assert "per_page" not in dict(req.url.params)
        return httpx.Response(200, json=_GAMES)

    assert mock(["game", "list", "--limit", "10"], handler).exit_code == 0


# --- issue #264: truncation footer, --offset, --title ----------------------
#
# The server caps `limit` at 500. `game list` rendered only data["games"] and
# ignored the meta envelope entirely, so a truncated table was indistinguishable
# from a complete one — and since `game list` is the only way to *discover* an
# id, ids past the cap were unreachable even after #260 fixed id lookup.


def _truncated(total: int = 3177, has_more: bool = True, offset: int = 0):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "games": _GAMES["games"],
                "meta": {"total": total, "has_more": has_more, "limit": 500, "offset": offset},
            },
        )

    return handler


def test_game_list_prints_truncation_footer_when_more_rows_exist(mock):
    r = mock(["game", "list"], _truncated())
    assert r.exit_code == 0
    assert "3177" in r.output  # the real total, not the rendered row count
    assert "--offset" in r.output  # tells the operator how to reach the rest


def test_game_list_footer_advertises_the_next_offset(mock):
    """The suggested offset must be offset+rendered, so following it pages
    forward rather than repeating the same window."""
    r = mock(["game", "list"], _truncated(offset=500))
    assert "502" in r.output  # 500 already skipped + 2 rendered here


def test_game_list_prints_no_footer_when_not_truncated(mock):
    r = mock(["game", "list"], _truncated(total=2, has_more=False))
    assert r.exit_code == 0
    assert "--offset" not in r.output


def test_game_list_tolerates_missing_meta(mock):
    """A response without meta (older server, or a proxy stripping it) must
    still render the table instead of raising KeyError."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"games": _GAMES["games"]})

    r = mock(["game", "list"], handler)
    assert r.exit_code == 0
    assert "CS2" in r.output


def test_game_list_offset_is_sent_to_the_server(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert dict(req.url.params)["offset"] == "500"
        return httpx.Response(200, json=_GAMES)

    assert mock(["game", "list", "--offset", "500"], handler).exit_code == 0


def test_game_list_omits_offset_when_not_requested(mock):
    """Default offset must not be sent as a param the server has to parse."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert "offset" not in dict(req.url.params)
        return httpx.Response(200, json=_GAMES)

    assert mock(["game", "list"], handler).exit_code == 0


def test_game_list_title_sends_contains_filter(mock):
    """--title is a substring search: it must go out as the server's
    `title_contains` op, not as an exact-match `title=` (which the games
    allow-list rejects with a 400)."""

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        assert params["title_contains"] == "fort"
        assert "title" not in params
        return httpx.Response(200, json=_GAMES)

    assert mock(["game", "list", "--title", "fort"], handler).exit_code == 0


def test_game_list_negative_offset_rejected_before_the_request(mock):
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a negative --offset must not reach the server")

    assert mock(["game", "list", "--offset", "-1"], handler).exit_code != 0


def _detail(req: httpx.Request) -> httpx.Response:
    """Serve GET /api/v1/games/{id} from _GAMES, 404 when the id is unknown.

    Any other path (e.g. a regression reintroducing the list scan) gets a plain
    404 rather than an exception, so the test fails on its own assertion instead
    of an opaque ValueError raised inside the transport.
    """
    # Dispatch on the full path shape, not just "last segment is numeric" — a
    # block-list route (…/block-list/steam/730) also ends in digits and would
    # otherwise be answered as a game lookup, giving a false pass.
    prefix = "/api/v1/games/"
    tail = req.url.path[len(prefix) :] if req.url.path.startswith(prefix) else ""
    if not tail.isdigit():
        return httpx.Response(404, json={"detail": "Not Found"})
    match = next((g for g in _GAMES["games"] if g["id"] == int(tail)), None)
    if match is None:
        return httpx.Response(404, json={"detail": "game not found"})
    return httpx.Response(200, json={"game": match})


def test_game_show_found(mock):
    r = mock(["game", "show", "2"], _detail)
    assert r.exit_code == 0
    assert "Turaco" in r.output


# (test_game_show_not_found_exits_1 removed: it asserted only exit_code == 1,
# which BOTH 404 branches produce, so it could not fail for any reason
# test_game_show_unknown_id_message_has_no_stale_pagination_wording — which
# asserts the same exit code plus the message content — does not already catch.)


def test_game_prefill_triggers(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/games/5/prefill"
        return httpx.Response(202, json={"job_id": 50})

    r = mock(["game", "prefill", "5"], handler)
    assert r.exit_code == 0 and "50" in r.output


def test_game_prefill_force_sends_force_param(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/games/5/prefill"
        assert dict(req.url.params).get("force") == "true"
        return httpx.Response(202, json={"job_id": 52})

    r = mock(["game", "prefill", "5", "--force"], handler)
    assert r.exit_code == 0 and "52" in r.output


def test_game_prefill_without_force_omits_param(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "force" not in dict(req.url.params)
        return httpx.Response(202, json={"job_id": 53})

    r = mock(["game", "prefill", "5"], handler)
    assert r.exit_code == 0


def test_game_validate_triggers(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/games/5/validate"
        return httpx.Response(202, json={"job_id": 51})

    r = mock(["game", "validate", "5"], handler)
    assert r.exit_code == 0 and "51" in r.output


def test_game_purge_triggers(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert req.url.path == "/api/v1/games/5/purge"
        return httpx.Response(202, json={"job_id": 55})

    r = mock(["game", "purge", "5"], handler)
    assert r.exit_code == 0 and "55" in r.output


def test_game_purge_rejects_non_positive_id(cli_invoke):
    r = cli_invoke(["game", "purge", "0"])
    assert r.exit_code != 0


def test_list_invalid_status_rejected_with_choices(cli_invoke):
    """Invalid --status must be rejected client-side, not silently empty (S11-E-04)."""
    r = cli_invoke(["game", "list", "--status", "uptodate"])  # valid is 'up_to_date'
    assert r.exit_code == 2
    assert "up_to_date" in (r.output + (r.stderr or ""))


def test_show_rejects_non_positive_id(cli_invoke):
    """game show 0 must give an actionable 'positive integer' message (S11-E-05)."""
    r = cli_invoke(["game", "show", "0"])
    assert r.exit_code == 2
    assert "positive" in (r.output + (r.stderr or "")).lower()


def test_game_block_resolves_and_posts(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games/5":
            return httpx.Response(
                200,
                json={
                    "game": {
                        "id": 5,
                        "platform": "steam",
                        "app_id": "730",
                        "title": "CS",
                        "status": "up_to_date",
                        "blocked": False,
                    }
                },
            )
        assert req.method == "POST" and req.url.path == "/api/v1/block-list"
        import json as _j

        assert _j.loads(req.content) == {
            "platform": "steam",
            "app_id": "730",
            "reason": "x",
            "source": "cli",
        }
        return httpx.Response(
            201,
            json={
                "id": 1,
                "platform": "steam",
                "app_id": "730",
                "reason": "x",
                "source": "cli",
                "blocked_at": "t",
            },
        )

    r = mock(["game", "block", "5", "--reason", "x"], handler)
    assert r.exit_code == 0 and "730" in r.output


def test_game_block_unknown_id_exit_1(mock):
    """A missing game exits 1 and surfaces the server's own wording. The CLI does
    not re-word it — doing so required classifying the status and conflated a
    missing game with a missing route (#261)."""
    r = mock(
        ["game", "block", "999"],
        lambda req: httpx.Response(404, json={"detail": "game not found"}),
    )
    out = r.output + (r.stderr or "")
    assert r.exit_code == 1
    assert "game not found" in out, out


def test_game_unblock_resolves_and_deletes(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games/5":
            return httpx.Response(
                200,
                json={
                    "game": {
                        "id": 5,
                        "platform": "steam",
                        "app_id": "730",
                        "title": "CS",
                        "status": "up_to_date",
                        "blocked": True,
                    }
                },
            )
        assert req.method == "DELETE" and req.url.path == "/api/v1/block-list/steam/730"
        return httpx.Response(200, json={"removed": 1})

    r = mock(["game", "unblock", "5"], handler)
    assert r.exit_code == 0


def test_game_list_shows_blocked_column(mock):
    games = {
        "games": [
            {
                "id": 5,
                "platform": "steam",
                "app_id": "730",
                "title": "CS",
                "status": "up_to_date",
                "blocked": True,
            }
        ],
        "meta": {},
    }
    r = mock(["game", "list"], lambda req: httpx.Response(200, json=games))
    assert r.exit_code == 0 and "BLOCKED" in r.output


def _detail_only(game: dict, on_action=None):
    """Handler where the *list* endpoint holds no rows but the detail endpoint
    resolves the game — i.e. an id past the server's 500-row list cap (#260).

    Resolving via the list is what the bug did, so a list-scanning CLI finds
    nothing here and exits 1; resolving via GET /games/{id} succeeds.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games":
            return httpx.Response(200, json={"games": [], "meta": {"total": 0}})
        if req.url.path == f"/api/v1/games/{game['id']}":
            return httpx.Response(200, json={"game": game})
        return (on_action or (lambda r: httpx.Response(200, json={})))(req)

    return handler


_FAR_GAME = {
    "id": 15488,
    "platform": "epic",
    "app_id": "4e980122452a4b48a99a83126e226053",
    "title": "Dead Cells",
    "status": "failed",
    "blocked": False,
}


def test_game_block_resolves_epic_app_id_via_detail_endpoint(mock):
    """#260: an id the list endpoint cannot return (past its 500-row cap) still
    resolves, and an Epic 32-hex app_id round-trips into the block-list body.

    (The sibling `unblock`/`show` "past the cap" cases were dropped as duplicates:
    the mock enforces no cap, so a high id is indistinguishable from a low one to
    the code under test, and both paths are already covered above.)
    """
    posted = {}

    def on_action(req: httpx.Request) -> httpx.Response:
        import json as _j

        assert req.method == "POST" and req.url.path == "/api/v1/block-list"
        posted.update(_j.loads(req.content))
        return httpx.Response(201, json={"id": 1, **posted, "blocked_at": "t"})

    r = mock(["game", "block", "15488", "--reason", "dead"], _detail_only(_FAR_GAME, on_action))
    assert r.exit_code == 0, r.output
    assert posted["platform"] == "epic"
    assert posted["app_id"] == "4e980122452a4b48a99a83126e226053"


def test_game_show_unknown_id_message_has_no_stale_pagination_wording(mock):
    """A genuinely missing id reports not-found without the '(in the first 500)'
    wording, which described the removed list-scan and misleads operators."""

    def handler(req: httpx.Request) -> httpx.Response:
        # List answers normally (empty) so a list-scanning CLI emits the stale
        # wording; the detail endpoint is the one reporting the real 404.
        # Detail text matches what the server actually sends (games router).
        if req.url.path == "/api/v1/games":
            return httpx.Response(200, json={"games": [], "meta": {"total": 0}})
        return httpx.Response(404, json={"detail": "game not found"})

    r = mock(["game", "show", "999"], handler)
    out = r.output + (r.stderr or "")
    assert r.exit_code == 1
    assert "first 500" not in out
    # The server's own wording is surfaced verbatim; the CLI no longer re-words
    # a 404 (that required classifying the status — see #261).
    assert "game not found" in out, out


def test_game_unblock_url_encodes_app_id(mock):
    """An app_id with a slash (Epic appName) must be percent-encoded so it stays
    a single path segment and doesn't mis-route (SEV-4 adversarial finding)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games/5":
            return httpx.Response(
                200,
                json={
                    "game": {
                        "id": 5,
                        "platform": "epic",
                        "app_id": "a/b",
                        "title": "X",
                        "status": "unknown",
                        "blocked": True,
                    }
                },
            )
        raw = req.url.raw_path.decode()
        assert "%2F" in raw.upper(), raw
        assert "/epic/a/b" not in raw
        return httpx.Response(200, json={"removed": 1})

    r = mock(["game", "unblock", "5"], handler)
    assert r.exit_code == 0


# --- review remediation (#261 adversarial review) -------------------------------
#
# The CLI must distinguish "this game does not exist" (the games router answers
# 404 {"detail": "game not found"}) from "this URL does not serve that route"
# (FastAPI's default 404 {"detail": "Not Found"}). Classifying by string-prefixing
# the ApiError message conflated the two and reported a misconfigured --url as a
# missing game — reintroducing the exact misdiagnosis #260 was filed to remove.


def test_route_level_404_is_not_reported_as_missing_game(mock):
    """A 404 from a wrong base URL / missing route must surface as an API error,
    NOT as 'game <id> not found' (which sends the operator hunting the wrong id)."""
    r = mock(
        ["game", "block", "15488"],
        lambda req: httpx.Response(404, json={"detail": "Not Found"}),
    )
    out = r.output + (r.stderr or "")
    assert r.exit_code == 1
    assert "not found" in out.lower()
    # The failure must not be attributed to the game id.
    assert "game 15488 not found" not in out, out
    assert "404" in out, out


def test_non_404_api_error_passes_through_unchanged(mock):
    """Regression guard: a 503 must keep the server's message. Without this, a
    later broadening of the except-block would turn an outage into 'not found'
    and send operators to hand-edit the block list."""
    r = mock(
        ["game", "block", "15488"],
        lambda req: httpx.Response(503, json={"detail": "database unavailable"}),
    )
    out = r.output + (r.stderr or "")
    assert r.exit_code == 1
    assert "database unavailable" in out, out
    assert "not found" not in out.lower(), out


def test_malformed_status_in_detail_response_is_an_error_not_a_fake_status(mock):
    """A null/non-str `status` is a malformed API response. It must trip the
    handles_api_errors backstop (exit 1), not render a fabricated '• NONE' at
    exit 0 that a script would read as a successful status query."""
    broken = {"id": 7, "platform": "steam", "app_id": "730", "title": "X", "status": None}
    r = mock(["game", "show", "7"], lambda req: httpx.Response(200, json={"game": broken}))
    assert r.exit_code == 1, r.output
    # The bug rendered the null as a status badge ("status  • NONE"); assert on
    # that rendering, not the bare substring (the correct AttributeError message
    # legitimately contains "NoneType").
    assert "• NONE" not in r.output.upper(), r.output


def test_null_app_id_is_not_coerced_into_a_literal_none_string(mock):
    """A null app_id must not be stringified to "None" and sent to the block-list
    endpoint — that targets nothing while reporting success."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games/7":
            broken = {
                "id": 7,
                "platform": "steam",
                "app_id": None,
                "title": "X",
                "status": "failed",
            }
            return httpx.Response(200, json={"game": broken})
        seen["path"] = req.url.path
        return httpx.Response(200, json={"removed": 0})

    r = mock(["game", "unblock", "7"], handler)
    assert r.exit_code == 1, r.output
    assert "None" not in seen.get("path", ""), seen


def test_game_show_renders_field_lines_not_the_raw_envelope(mock):
    """Pins `game show`'s rendering contract. Without this, dropping the
    data["game"] unwrap still passes every other show test, because they are
    substring checks that also match the envelope's dict repr."""
    r = mock(["game", "show", "2"], _detail)
    assert r.exit_code == 0, r.output
    assert "title              Turaco" in r.output, r.output
    assert "{'id'" not in r.output, r.output


# --- round-3 remediation (#261 re-review) ---------------------------------------
#
# The first remediation guarded app_id only by accident (quote() raises on None)
# and left `platform` interpolated raw, so unblock still issued
# DELETE /block-list/None/<app_id> at exit 0. Validate the resolved identity at
# the source instead of relying on what a downstream call happens to reject.


def _game_body(**over):
    game = {"id": 7, "platform": "steam", "app_id": "730", "title": "X", "status": "failed"}
    game.update(over)
    return {"game": game}


def _detail_7(body, on_action=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games/7":
            return httpx.Response(200, json=body)
        return (on_action or (lambda r: httpx.Response(200, json={"removed": 0})))(req)

    return handler


def test_unblock_rejects_null_platform_instead_of_targeting_none(mock):
    """A null platform must not reach the block-list path as the literal 'None'."""
    seen = {}

    def on_action(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json={"removed": 0})

    r = mock(["game", "unblock", "7"], _detail_7(_game_body(platform=None), on_action))
    assert r.exit_code == 1, r.output
    assert "path" not in seen, seen


def test_block_rejects_null_app_id_instead_of_posting_null(mock):
    """`block` must not POST a null app_id — it has no quote() to raise for it."""
    seen = {}

    def on_action(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(201, json={})

    r = mock(["game", "block", "7"], _detail_7(_game_body(app_id=None), on_action))
    assert r.exit_code == 1, r.output
    assert "path" not in seen, seen


def test_game_show_writes_nothing_when_a_field_is_malformed(mock):
    """Output must be all-or-nothing: a malformed field must not leave a partial
    record on stdout that a redirect would capture as a valid short record."""
    r = mock(["game", "show", "7"], lambda req: httpx.Response(200, json=_game_body(status=5)))
    assert r.exit_code == 1, r.output
    assert "platform" not in r.output, r.output
    assert "730" not in r.output, r.output


# --- simplification round (#261) -------------------------------------------------


def test_unblock_percent_encodes_platform_like_app_id(mock):
    """`platform` is interpolated into the DELETE path just like app_id, so it
    must be encoded too. Unencoded, a '/' or '#' in that field re-targets the
    request at a DIFFERENT block-list row while reporting success."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games/7":
            return httpx.Response(200, json=_game_body(platform="epic/OTHER#"))
        seen["raw"] = req.url.raw_path.decode()
        return httpx.Response(200, json={"removed": 1})

    mock(["game", "unblock", "7"], handler)
    raw = seen.get("raw", "")
    assert "/api/v1/block-list/epic/OTHER" not in raw, raw
    assert "%2F" in raw.upper() or raw == "", raw


def test_resolve_rejects_empty_string_platform(mock):
    """Pins the empty-string half of the guard (the isinstance half is covered
    by the null cases; deleting `or not platform` must fail something)."""
    seen = {}

    def on_action(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json={"removed": 0})

    r = mock(["game", "unblock", "7"], _detail_7(_game_body(platform=""), on_action))
    assert r.exit_code == 1, r.output
    assert "path" not in seen, seen


def test_resolve_rejects_empty_string_app_id(mock):
    """Pins the empty-string half of the app_id guard."""
    seen = {}

    def on_action(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(201, json={})

    r = mock(["game", "block", "7"], _detail_7(_game_body(app_id=""), on_action))
    assert r.exit_code == 1, r.output
    assert "path" not in seen, seen
