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

    def test_a_tally_under_the_threshold_is_up_but_carries_the_numbers(self) -> None:
        ok, msg = summarise_tally(
            {"fetched": 48, "skipped": 1477, "failed": 698, "apps": 1174}, 0.75
        )

        assert ok is True
        assert "failed=698" in msg
        assert "apps=1174" in msg

    def test_the_depotdownloader_era_tally_is_down_at_the_default(self) -> None:
        """#361 I2: 698/1174 (0.59) was DepotDownloader's rate-limited steady state.
        With one login per run it is a collapse, so the default must alarm on it."""
        from orchestrator.core.settings import Settings

        ratio = Settings(orchestrator_token="a" * 32).fetch_manifests_max_failure_ratio
        tally = {"fetched": 48, "skipped": 1477, "failed": 698, "apps": 1174}
        ok, _ = summarise_tally(tally, ratio)

        assert ok is False

    def test_sixteen_and_a_half_percent_failed_is_down_at_ten_percent(self) -> None:
        tally = {"fetched": 1011, "skipped": 0, "failed": 200, "apps": 1211}
        ok, msg = summarise_tally(tally, 0.10)

        assert ok is False
        assert "failed=200" in msg

    @pytest.mark.parametrize(
        ("failed", "expected_ok"),
        [(59, True), (84, True), (85, False)],
        ids=["first_live_run_4.9pct", "6.94pct_is_up", "7.02pct_is_down"],
    )
    def test_the_default_alarm_boundary_sits_at_seven_percent(
        self, failed: int, expected_ok: bool
    ) -> None:
        """#361 (Karl, 2026-10-08): the first live run measured 59/1211 = 4.9% (job
        46856). 84/1211 = 6.94% is still UP and 85/1211 = 7.02% is DOWN."""
        from orchestrator.core.settings import Settings

        ratio = Settings(orchestrator_token="a" * 32).fetch_manifests_max_failure_ratio
        tally = {"fetched": 1211 - failed, "skipped": 0, "failed": failed, "apps": 1211}
        ok, msg = summarise_tally(tally, ratio)

        assert ok is expected_ok
        assert f"failed={failed}" in msg

    def test_a_ratio_exactly_at_the_threshold_is_still_up(self) -> None:
        """Down means strictly above the ratio; 1/4 == 0.25 exactly in float."""
        ok, _ = summarise_tally({"fetched": 3, "skipped": 0, "failed": 1, "apps": 4}, 0.25)

        assert ok is True

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

    @pytest.mark.parametrize(
        "malformed",
        [
            {"apps": "n/a"},
            {"apps": [1, 2]},
            {"apps": 10, "failed": None},
            {"apps": {"count": 3}},
            {"failed": "many", "apps": 10},
        ],
        ids=["str_apps", "list_apps", "none_failed", "dict_apps", "str_failed"],
    )
    def test_a_malformed_tally_reads_as_healthy_rather_than_raising(self, malformed) -> None:
        """The docstring promises this and the code did not deliver it.

        int("n/a") raises ValueError and int([1,2]) raises TypeError, and either
        propagates out of the handler — so the worker marks the JOB failed and
        pushes the heartbeat DOWN. That manufactures precisely the false alarm the
        contract forswears, from nothing worse than a version skew between the LXC
        and the NAS changing a field's type.
        """
        ok, msg = summarise_tally(malformed, 0.75)

        assert ok is True, "an unparseable tally is not evidence of failure"
        assert msg, "it must still report something"


@pytest.mark.asyncio
class TestHandlerReturnsTheSummary:
    async def test_the_handler_hands_its_tally_back_for_the_heartbeat(self) -> None:
        from orchestrator.jobs.handlers.fetch_manifests import fetch_manifests_handler
        from orchestrator.jobs.worker import Deps

        class _Agent:
            async def fetch_manifests(self):
                return {"fetched": 48, "skipped": 1477, "failed": 50, "apps": 1174}

        result = await fetch_manifests_handler({"id": 1}, Deps(pool=None, agent_client=_Agent()))

        assert result is not None, (
            "the handler must return its tally, or the worker has nothing to put in the "
            "heartbeat and the monitor goes green over a half-failing run"
        )
        assert result.ok is True
        assert "failed=50" in result.msg

    async def test_the_handler_reports_down_above_the_default_threshold(self) -> None:
        from orchestrator.jobs.handlers.fetch_manifests import fetch_manifests_handler
        from orchestrator.jobs.worker import Deps

        class _Agent:
            async def fetch_manifests(self):
                return {"fetched": 1011, "skipped": 0, "failed": 200, "apps": 1211}

        result = await fetch_manifests_handler({"id": 1}, Deps(pool=None, agent_client=_Agent()))

        assert result is not None
        assert result.ok is False
