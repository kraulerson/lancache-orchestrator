"""A manifest that yields no chunks is "I could not tell", not "fully cached".

UAT-14 #292, found by the exploratory arm. Both agent routers classified a
zero-chunk result as ``cached``, with a comment defending it: "the located manifests
contained no chunks — nothing to cache, so the app is up to date". That reasoning
conflates two very different states:

  * this app genuinely has nothing to cache
  * I could not read what this app needs

A zero-byte ``.bin``, three bytes of garbage, an empty ``.shas`` and a well-formed
Epic manifest with an empty ``ChunkHashList`` all land in the second case, and all
came back ``outcome="cached", chunks_total=0`` — which ``validate.py`` maps to
``games.status='up_to_date'``. A game went green on the strength of a zero-byte file.

``error`` is the honest answer, and it is also the SAFE one: the ``_STATUS_FOR`` map in
``orchestrator.jobs.measurement`` deliberately omits ``error``, so an unreadable manifest
leaves ``games.status`` untouched rather than flipping it green OR falsely failing a
healthy game. The fault is recorded in validation_history and nothing is asserted that
cannot be backed up.
"""

from __future__ import annotations

from orchestrator.agent.routers.epic import _classify as epic_classify
from orchestrator.agent.routers.steam import _classify as steam_classify


class TestZeroChunksIsNotCached:
    """HONEST SCOPE for the steam case: since the all-redist fix, steam_validate's
    final return only runs when included > 0, and `included` is incremented only
    after `total += len(dpaths)` on a non-empty depot — so total >= 1 there and
    `steam_classify(0, ·)` is UNREACHABLE from the endpoint. This pins the function's
    contract, not the live path.

    The endpoint-level guard is
    tests/agent/test_steam_validate.py::test_validate_empty_manifest_is_error_at_the_endpoint,
    which caught a regression this file could not: keying the all-redist branch on
    `parsed_ok` made a zero-byte manifest report 'cached' again, because the parsers
    never raise. The epic case below IS on the live path.
    """

    def test_steam_zero_total_is_an_error_not_cached(self) -> None:
        assert steam_classify(0, 0) == "error", (
            "zero chunks means the manifest could not be read, which is indistinguishable "
            "from an empty app — reporting 'cached' asserts something we cannot back up"
        )

    def test_epic_zero_total_is_an_error_not_cached(self) -> None:
        assert epic_classify(0, 0) == "error"


class TestRealVerdictsAreUnchanged:
    """The fix must not disturb any classification that was already correct."""

    def test_steam_everything_present_is_cached(self) -> None:
        assert steam_classify(337, 337) == "cached"

    def test_steam_nothing_present_is_missing(self) -> None:
        assert steam_classify(337, 0) == "missing"

    def test_steam_some_present_is_partial(self) -> None:
        assert steam_classify(337, 12) == "partial"

    def test_epic_everything_present_is_cached(self) -> None:
        assert epic_classify(50, 50) == "cached"

    def test_epic_nothing_present_is_missing(self) -> None:
        assert epic_classify(50, 0) == "missing"

    def test_epic_some_present_is_partial(self) -> None:
        assert epic_classify(50, 3) == "partial"


class TestErrorDoesNotChangeGameStatus:
    """Why 'error' is the safe answer as well as the honest one."""

    def test_the_status_map_has_no_entry_for_error(self) -> None:
        from orchestrator.jobs.measurement import _STATUS_FOR

        assert "error" not in _STATUS_FOR, (
            "an unreadable manifest must leave games.status alone. If 'error' ever "
            "gains a mapping, a zero-chunk result starts overwriting real state again "
            "and #292 returns by a different route."
        )
