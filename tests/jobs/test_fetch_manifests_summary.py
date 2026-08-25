"""fetch_manifests reports its tally to the monitor instead of throwing it away.

UAT-14 #294. The live arm found the job recording `succeeded` while 669 of 1170 apps
failed, with the failures visible only in the agent's warning stream. A heartbeat
pushed on that job would have broadcast a confident green over a half-failing run.

The job state stays `succeeded`, deliberately. `succeeded` means *the work ran*, and
it did — the agent was dispatched and returned a tally. Per-app failures are data, not
a job fault, and 669 failures is the steady state against 98.7% manifest coverage, so
failing the job on them would make `failed` mean nothing in the other direction: an
alert that is always red is exactly as useless as one that is always green.

So the tally travels to the monitor, where a human can see it, and the heartbeat only
goes DOWN when the failure ratio crosses a threshold.
"""

from __future__ import annotations

import pytest

from orchestrator.jobs.handlers.fetch_manifests import summarise_tally


class TestSummariseTally:
    """Pure: a tally plus a threshold becomes an up/down and a human-readable line."""

    def test_a_clean_run_is_up_and_says_so(self) -> None:
        ok, msg = summarise_tally({"fetched": 40, "skipped": 1100, "failed": 0, "apps": 1140}, 0.75)

        assert ok is True
        assert "fetched=40" in msg
        assert "failed=0" in msg

    def test_todays_real_tally_is_up_but_carries_the_numbers(self) -> None:
        """698/1174 is the current steady state — visible, not alarming."""
        ok, msg = summarise_tally(
            {"fetched": 48, "skipped": 1477, "failed": 698, "apps": 1174}, 0.75
        )

        assert ok is True, "the steady state must not hold the monitor permanently red"
        assert "failed=698" in msg
        assert "apps=1174" in msg

    def test_crossing_the_threshold_is_down(self) -> None:
        ok, msg = summarise_tally({"fetched": 0, "skipped": 0, "failed": 900, "apps": 1000}, 0.75)

        assert ok is False
        assert "failed=900" in msg

    def test_the_threshold_is_a_ratio_of_apps_not_an_absolute_count(self) -> None:
        """A big library and a small one should alarm at the same proportion."""
        small = summarise_tally({"fetched": 1, "skipped": 0, "failed": 9, "apps": 10}, 0.75)
        large = summarise_tally({"fetched": 100, "skipped": 0, "failed": 900, "apps": 1000}, 0.75)

        assert small[0] is False
        assert large[0] is False

    def test_zero_apps_is_up_rather_than_a_division_by_zero(self) -> None:
        ok, msg = summarise_tally({"fetched": 0, "skipped": 0, "failed": 0, "apps": 0}, 0.75)

        assert ok is True
        assert msg, "an empty run still reports something"

    def test_a_missing_key_does_not_explode(self) -> None:
        """The agent's tally shape is not ours to guarantee."""
        ok, msg = summarise_tally({}, 0.75)

        assert ok is True
        assert msg


@pytest.mark.asyncio
class TestHandlerReturnsTheSummary:
    async def test_the_handler_hands_its_tally_back_for_the_heartbeat(self) -> None:
        from orchestrator.jobs.handlers.fetch_manifests import fetch_manifests_handler
        from orchestrator.jobs.worker import Deps

        class _Agent:
            async def fetch_manifests(self):
                return {"fetched": 48, "skipped": 1477, "failed": 698, "apps": 1174}

        result = await fetch_manifests_handler({"id": 1}, Deps(pool=None, agent_client=_Agent()))

        assert result is not None, (
            "the handler must return its tally, or the worker has nothing to put in the "
            "heartbeat and the monitor goes green over a half-failing run"
        )
        assert result.ok is True
        assert "failed=698" in result.msg
