"""#229: classify a Steam app as a prefill-exclusion CANDIDATE (soundtrack /
tool / SDK / dedicated server / demo / video) — never applied automatically."""

from __future__ import annotations

import pytest

from orchestrator.platform.steam.selection_classifier import classify


@pytest.mark.parametrize(
    "app_type,name,expected_prefix",
    [
        ("music", "Celeste Original Soundtrack", "type=music"),
        ("application", "RPG Maker VX Ace", "type=application"),
        ("tool", "Source SDK", "type=tool"),
        ("demo", "Some Game Demo", "type=demo"),
        ("video", "Making Of", "type=video"),
        ("MUSIC", "Loud Case", "type=music"),  # case-insensitive
    ],
)
def test_non_game_types_are_candidates(app_type, name, expected_prefix):
    assert classify(app_type, name).startswith(expected_prefix)


@pytest.mark.parametrize(
    "app_type,name",
    [
        ("game", "Portal"),
        ("game", "Half-Life 2"),
        ("dlc", "Portal 2 - DLC Pack"),  # DLC is real content — kept
        ("mod", "Garry's Mod thing"),
        ("game", ""),  # no name, game type -> keep
        # #229 follow-up: Steam types some REAL games' app_ids as `advertising`
        # (seen live: Darksiders II 50650, Eufloria 41210). Dropped from the
        # exclude set so the classifier stops flagging real games.
        ("advertising", "Darksiders II"),
        ("advertising", "Eufloria"),
    ],
)
def test_real_games_are_not_candidates(app_type, name):
    assert classify(app_type, name) is None


@pytest.mark.parametrize(
    "name,flag",
    [
        ("Half-Life Dedicated Server", "dedicated server"),
        ("Left 4 Dead Dedicated Server", "dedicated server"),
        ("GameGuru SDK", "sdk"),
        ("Portal Soundtrack", "soundtrack"),
        ("Deep Rock Galactic - OST", "ost"),
        ("CPU Benchmark", "benchmark"),
    ],
)
def test_name_flags_catch_tools_typed_as_game(name, flag):
    # These are type=game on Steam but are really servers/tools/soundtracks.
    reason = classify("game", name)
    assert reason is not None
    assert flag in reason.lower()


def test_empty_type_and_name_is_not_a_candidate():
    assert classify("", "") is None
    assert classify(None, None) is None  # tolerate NULLs from the DB


def test_server_substring_does_not_overmatch_a_real_game():
    # "Observer" contains "server" — must NOT be flagged (we match the full
    # phrase "dedicated server", not a bare "server").
    assert classify("game", ">observer_") is None


# --- MP-only detection (Karl 2026-07-04: exclude multiplayer-only games) -------


def test_multiplayer_only_game_is_a_candidate():
    # A `game` with a multiplayer category and NO single-player category (Dota 2).
    reason = classify("game", "Dota 2", has_single_player=0, has_multiplayer=1)
    assert reason == "multiplayer-only"


def test_single_and_multiplayer_game_is_kept():
    # Portal 2: has BOTH single- and multi-player -> a real SP game, keep it.
    assert classify("game", "Portal 2", has_single_player=1, has_multiplayer=1) is None


def test_single_player_only_game_is_kept():
    assert classify("game", "Stardew Valley", has_single_player=1, has_multiplayer=0) is None


def test_unknown_flags_never_flag_mp_only():
    # Categories not yet fetched (NULL) -> never guess MP-only.
    assert classify("game", "Some Game", has_single_player=None, has_multiplayer=None) is None
    # Multiplayer known but single-player unknown -> don't flag (might have SP).
    assert classify("game", "Some Game", has_single_player=None, has_multiplayer=1) is None


def test_no_gameplay_categories_is_kept():
    # Categories fetched but neither SP nor MP present (e.g. only Trading Cards).
    assert classify("game", "Weird App", has_single_player=0, has_multiplayer=0) is None


def test_non_game_type_wins_over_mp_flags():
    # A soundtrack that somehow also carries an MP category is still `type=music`.
    assert classify("music", "Some OST", has_single_player=0, has_multiplayer=1) == "type=music"
