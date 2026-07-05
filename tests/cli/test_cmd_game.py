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


def test_game_show_found(mock):
    r = mock(["game", "show", "2"], lambda req: httpx.Response(200, json=_GAMES))
    assert r.exit_code == 0
    assert "Turaco" in r.output


def test_game_show_not_found_exits_1(mock):
    r = mock(["game", "show", "999"], lambda req: httpx.Response(200, json=_GAMES))
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
        if req.url.path == "/api/v1/games":
            return httpx.Response(
                200,
                json={
                    "games": [
                        {
                            "id": 5,
                            "platform": "steam",
                            "app_id": "730",
                            "title": "CS",
                            "status": "up_to_date",
                            "blocked": False,
                        }
                    ],
                    "meta": {},
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
        ["game", "block", "999"], lambda req: httpx.Response(200, json={"games": [], "meta": {}})
    )
    assert r.exit_code == 1


def test_game_unblock_resolves_and_deletes(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games":
            return httpx.Response(
                200,
                json={
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


def test_game_unblock_url_encodes_app_id(mock):
    """An app_id with a slash (Epic appName) must be percent-encoded so it stays
    a single path segment and doesn't mis-route (SEV-4 adversarial finding)."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/games":
            return httpx.Response(
                200,
                json={
                    "games": [
                        {
                            "id": 5,
                            "platform": "epic",
                            "app_id": "a/b",
                            "title": "X",
                            "status": "unknown",
                            "blocked": True,
                        }
                    ],
                    "meta": {},
                },
            )
        raw = req.url.raw_path.decode()
        assert "%2F" in raw.upper(), raw
        assert "/epic/a/b" not in raw
        return httpx.Response(200, json={"removed": 1})

    r = mock(["game", "unblock", "5"], handler)
    assert r.exit_code == 0
