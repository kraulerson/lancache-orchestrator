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


def test_game_validate_triggers(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/games/5/validate"
        return httpx.Response(202, json={"job_id": 51})

    r = mock(["game", "validate", "5"], handler)
    assert r.exit_code == 0 and "51" in r.output


def test_game_manifest_triggers(mock):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/games/5/manifest/fetch"
        return httpx.Response(202, json={"job_id": 52})

    r = mock(["game", "manifest", "5"], handler)
    assert r.exit_code == 0 and "52" in r.output
