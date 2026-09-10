"""The validate budget must be calibrated against real throughput, not a constant.

2026-09-01. The OMV rebuild of the NAS removed the bcache layer that fronted the
RAID0, and validation — which is stat-heavy random metadata I/O — went from a
measured 1,471-1,539 chunks/sec to 48-54. Same code, same games; the hardware
underneath changed.

``VALIDATE_TIMEOUT_PER_CHUNK_SEC = 0.00217`` encodes an assumption of ~461
chunks/sec. At 50/sec the budget is roughly 9x short, and the crossover where a
game can no longer finish inside its own budget is::

    chunks / 50 > 300 + 0.00217 * chunks   ->   fails above ~16,826 chunks

That is 379 of 1,811 games (21%), including ARK: Survival Evolved (369,317
chunks), ARK ModKit UE4 (359,671), Warhammer 40,000: Darktide (310,059) and God
of War (216,751). Confirmed live: Ghostwire Tokyo (~24,000 chunks) failed with
``AgentError: agent unreachable: ReadTimeout`` after 352 s, while Mortal Shell
(14,142 chunks) succeeded — exactly straddling the predicted line.

Those games cannot self-correct: a timed-out validate writes NO validation_history
row, so the game keeps whatever status it had. They stay wrong forever.

The fix is not another hand-tuned constant — that is what broke. The rate is
expressed as an explicit assumed throughput so the next hardware change is a
config edit rather than an incident.
"""

from __future__ import annotations

import pytest

from orchestrator.clients.agent_client import (
    VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC,
    VALIDATE_TIMEOUT_BASE_SEC,
    VALIDATE_TIMEOUT_CEILING_SEC,
    validate_timeout_for,
)

# The largest game on record, from the live DB on 2026-09-01.
LARGEST_GAME_CHUNKS = 369_317
# Measured post-rebuild throughput, from validation_history durations.
MEASURED_CHUNKS_PER_SEC = 48.0


class TestTheRateIsAnExplicitThroughputAssumption:
    def test_the_assumed_throughput_is_stated_not_buried_in_a_per_chunk_constant(
        self,
    ) -> None:
        """A reader must be able to see what hardware speed the budget assumes."""
        assert VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC > 0

    def test_the_assumption_is_not_faster_than_the_hardware_measured(self) -> None:
        """The old 0.00217 s/chunk assumed ~461/sec against a real 48-54. Assuming a
        rate the disks cannot deliver is precisely the bug."""
        assert VALIDATE_TIMEOUT_ASSUMED_CHUNKS_PER_SEC <= MEASURED_CHUNKS_PER_SEC

    def test_the_throughput_can_be_overridden_per_call(self) -> None:
        """Hardware changes. This must not require a code edit next time."""
        slow = validate_timeout_for(100_000, chunks_per_sec=10.0).read
        fast = validate_timeout_for(100_000, chunks_per_sec=1000.0).read
        assert slow > fast


class TestEveryRealGameFitsInsideItsBudget:
    @pytest.mark.parametrize(
        ("chunks", "title"),
        [
            (369_317, "ARK: Survival Evolved"),
            (359_671, "ARK ModKit (UE4)"),
            (310_059, "Warhammer 40,000: Darktide"),
            (216_843, "ARK: Survival Ascended"),
            (216_751, "God of War"),
            (24_000, "Ghostwire Tokyo"),
            (16_827, "one chunk past the old crossover"),
        ],
    )
    def test_the_budget_covers_the_time_the_work_actually_takes(
        self, chunks: int, title: str
    ) -> None:
        need = chunks / MEASURED_CHUNKS_PER_SEC
        budget = validate_timeout_for(chunks).read
        assert budget >= need, (
            f"{title}: {chunks} chunks needs {need:.0f}s at the measured "
            f"{MEASURED_CHUNKS_PER_SEC:.0f}/s but is given {budget:.0f}s"
        )

    def test_the_ceiling_does_not_clip_the_largest_real_game(self) -> None:
        """A ceiling below the biggest game reintroduces the class of bug: that game
        can never validate, and never self-corrects, however long it runs."""
        assert VALIDATE_TIMEOUT_CEILING_SEC >= LARGEST_GAME_CHUNKS / MEASURED_CHUNKS_PER_SEC


class TestTheOldGuaranteesStillHold:
    def test_an_unknown_chunk_count_keeps_the_base_budget(self) -> None:
        assert validate_timeout_for(None).read == VALIDATE_TIMEOUT_BASE_SEC

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_nonsense_count_falls_back_to_the_base(self, bad: int) -> None:
        assert validate_timeout_for(bad).read == VALIDATE_TIMEOUT_BASE_SEC

    def test_a_small_game_never_gets_less_than_the_base(self) -> None:
        assert validate_timeout_for(50).read >= VALIDATE_TIMEOUT_BASE_SEC

    def test_it_still_grows_with_the_count(self) -> None:
        assert validate_timeout_for(200_000).read > validate_timeout_for(20_000).read

    def test_it_is_still_capped(self) -> None:
        """A wedged agent must still surface as a failure rather than hang forever."""
        assert validate_timeout_for(500_000_000).read == VALIDATE_TIMEOUT_CEILING_SEC

    def test_connect_timeout_stays_short(self) -> None:
        """Reaching the agent is fast or it is broken — only the WORK is slow."""
        assert validate_timeout_for(LARGEST_GAME_CHUNKS).connect == 10.0
