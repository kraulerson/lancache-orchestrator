"""The validate timeout scales with the work, instead of one number for every game.

UAT-14 #297. Every validate call used ``httpx.Timeout(300.0)`` regardless of size.
Game 15035 "ARK ModKit (UE4)" is 359,671 chunks and takes about 433 s to disk-stat,
so it was cut off every single time — 27 sweeps, 52 days unvalidated, while still
reporting ``up_to_date``.

Cutting it off does not merely discard finished work: the live arm proved the agent's
handler is torn down on client disconnect, so the run is ABORTED. Nothing is salvaged,
and the next sweep starts from scratch and fails the same way.

A bigger fixed number would only move the cliff — MechWarrior 5 Editor already sits at
271 s against the 300 s limit. Scaling by the chunk count the orchestrator already
holds in ``manifests.chunk_count`` removes the class.
"""

from __future__ import annotations

import pytest

from orchestrator.clients.agent_client import (
    VALIDATE_TIMEOUT_BASE_SEC,
    VALIDATE_TIMEOUT_CEILING_SEC,
    validate_timeout_for,
)


class TestScaling:
    def test_an_unknown_chunk_count_keeps_the_existing_budget(self) -> None:
        """No information means no change — steam does not know its count up front."""
        assert validate_timeout_for(None).read == VALIDATE_TIMEOUT_BASE_SEC

    def test_a_small_game_gets_at_least_the_base_budget(self) -> None:
        """Scaling must never make anything worse than it is today."""
        assert validate_timeout_for(50).read >= VALIDATE_TIMEOUT_BASE_SEC

    def test_ark_modkit_gets_comfortably_more_than_it_measured(self) -> None:
        """359,671 chunks measured ~433 s, with a range up to 876 s on a bad day."""
        budget = validate_timeout_for(359_671).read

        assert budget > 433, f"must exceed the measured requirement, got {budget}"
        assert budget >= 876, (
            f"must cover the worst measured run, not just the average, got {budget}"
        )

    def test_mechwarrior_sized_games_gain_headroom(self) -> None:
        """The one already at 271 s against a 300 s limit — one slow NFS day away."""
        assert validate_timeout_for(20_000).read > VALIDATE_TIMEOUT_BASE_SEC

    def test_the_budget_is_capped(self) -> None:
        """An absurd count must not produce an effectively infinite wait: a genuinely
        wedged agent still has to surface as a failure rather than hang the sweep."""
        assert validate_timeout_for(500_000_000).read == VALIDATE_TIMEOUT_CEILING_SEC

    def test_the_ceiling_still_bounds_a_wedged_agent(self) -> None:
        """SUPERSEDED 2026-09-01. This asserted ``<= 1800`` on the reasoning that
        "the sweep runs every 6 hours and takes ~40 minutes, so one game must not
        consume the window". That premise died with the OMV rebuild: validation
        dropped from ~1500 to ~50 chunks/sec, no full sweep fits in 6 hours at any
        ceiling, and a 1800s cap meant 379 of 1811 games could never validate at
        all — permanently, since a timed-out validate writes no history row.

        The ceiling's real job is narrower: stop a genuinely wedged agent hanging
        forever. It must therefore be finite and not absurd, but it is no longer a
        scheduling constraint. Pacing belongs to the sweep, not to this constant.
        """
        assert 0 < VALIDATE_TIMEOUT_CEILING_SEC <= 86_400

    def test_it_grows_with_the_count(self) -> None:
        assert validate_timeout_for(200_000).read > validate_timeout_for(20_000).read

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_nonsense_count_falls_back_to_the_base(self, bad: int) -> None:
        assert validate_timeout_for(bad).read == VALIDATE_TIMEOUT_BASE_SEC

    def test_connect_timeout_stays_short(self) -> None:
        """Reaching the agent is fast or it is broken — only the WORK is slow."""
        assert validate_timeout_for(359_671).connect == 10.0
