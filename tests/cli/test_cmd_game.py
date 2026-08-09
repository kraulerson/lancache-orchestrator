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


def _detail(req: httpx.Request) -> httpx.Response:
    """Serve GET /api/v1/games/{id} from _GAMES, 404 when the id is unknown.

    Any other path (e.g. a regression reintroducing the list scan) gets a plain
    404 rather than an exception, so the test fails on its own assertion instead
    of an opaque ValueError raised inside the transport.
    """
    tail = req.url.path.rsplit("/", 1)[-1]
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


def test_game_show_not_found_exits_1(mock):
    r = mock(["game", "show", "999"], _detail)
    assert r.exit_code == 1


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
    r = mock(
        ["game", "block", "999"],
        lambda req: httpx.Response(404, json={"detail": "No game with that id"}),
    )
    assert r.exit_code == 1


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
    assert "999" in out, "not-found message should name the id the operator typed"


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


def test_game_not_found_404_still_reports_the_game_id(mock):
    """The genuine missing-game 404 keeps its operator-friendly message."""
    r = mock(
        ["game", "block", "15488"],
        lambda req: httpx.Response(404, json={"detail": "game not found"}),
    )
    out = r.output + (r.stderr or "")
    assert r.exit_code == 1
    assert "game 15488 not found" in out, out


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
