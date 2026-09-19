"""The keys_zone gauge's decision logic.

The 2026-07-31 mass deletion was nginx's cache-manager evicting live game data
from a ~94%-full keys_zone. `df` cannot see that metric and nginx OSS publishes
no gauge for it — there is no `http_api_module`, `stub_status` omits cache zones,
and the error log stayed silent through the entire nine-day incident because
nginx only logs when a forced expire fails outright, never the normal
evict-to-fit path.

So the metric is derived, and a derived metric is only as trustworthy as its
degenerate cases. That is what most of this file tests: an unreadable directory
is not an empty one, a failed sample is not a healthy sample, and an unknown
trend is not a safe trend.

Dependency-free on purpose. `fanotify_guard.py` loads `libc.so.6` at import and
cannot run on a developer machine, so every decision that can be arithmetically
wrong lives in a module that CI can import — the same split that produced
`delete_actor.py`.
"""

from __future__ import annotations

import pytest
from tools.cache_catcher.key_budget import (
    KEYS_PER_MB,
    TOTAL_LEAVES,
    effective_capacity,
    project_days_to,
    ram_capacity_keys,
    summarise_sample,
    verdict,
    zone_capacity_keys,
)

DAY = 86400.0

# The live ceilings measured on 2026-09-18: the configured zone holds 80M keys,
# but 9 GiB of RAM at the measured 146 B/key only holds ~66M. RAM binds. See #346.
ZONE_KEYS = 80_000_000
RAM_KEYS = 66_000_000


def _sample(objects):
    """A clean sample of `objects` total, spread evenly over 256 read leaves."""
    per_leaf = int(objects / TOTAL_LEAVES)
    return summarise_sample([per_leaf] * 256, read_failures=0)


# --- sampling -------------------------------------------------------------


def test_the_estimate_scales_the_mean_leaf_to_the_whole_tree():
    s = summarise_sample([10, 20, 30])
    assert s.objects == pytest.approx(20 * TOTAL_LEAVES)
    assert s.leaves_read == 3


def test_an_unreadable_leaf_is_a_read_failure_not_an_empty_one():
    """The measurement that produced this design first came out 13x low because a
    permission-denied directory read looked exactly like an empty directory —
    some cache leaves are mode 0700. A denied leaf must never drag the mean
    down; it is an absence of information, not an absence of files."""
    s = summarise_sample([500, 500], read_failures=2)
    assert s.objects == pytest.approx(500 * TOTAL_LEAVES)
    assert s.read_failures == 2
    assert s.leaves_read == 2


def test_a_sample_that_read_nothing_reports_unknown_rather_than_zero():
    """Zero objects and 'I could not look' are different facts. Reporting zero
    would read as a catastrophically empty cache and, worse, would sit far below
    every threshold and trip nothing at all."""
    s = summarise_sample([], read_failures=256)
    assert s.objects is None
    assert s.stderr is None


def test_the_standard_error_shrinks_as_more_leaves_are_read():
    few = summarise_sample([100, 900] * 5)
    many = summarise_sample([100, 900] * 50)
    assert many.stderr < few.stderr


def test_a_single_leaf_admits_it_cannot_estimate_error():
    """One sample point has no spread to measure. Reporting a small error would
    be false precision."""
    s = summarise_sample([500])
    assert s.objects == pytest.approx(500 * TOTAL_LEAVES)
    assert s.stderr == float("inf")


# --- capacities -----------------------------------------------------------


def test_zone_capacity_uses_nginx_documented_key_density():
    assert zone_capacity_keys(10000) == 10000 * KEYS_PER_MB == 80_000_000


@pytest.mark.parametrize("bad", [None, 0, -1], ids=["none", "zero", "negative"])
def test_an_unreadable_index_size_reports_unknown_not_a_guess(bad):
    """CACHE_INDEX_SIZE is read from the running nginx process. If that read
    fails we do not invent a number — #315 is an open bug about exactly this
    shape, a bare constant standing in for a configurable value."""
    assert zone_capacity_keys(bad) is None


def test_ram_capacity_divides_the_budget_by_measured_cost_per_key():
    assert ram_capacity_keys(9 * 1024**3, 146.0) == pytest.approx(66_182_878, rel=1e-3)


@pytest.mark.parametrize(
    "budget,per_key",
    [(None, 146.0), (9 * 1024**3, None), (9 * 1024**3, 0.0)],
    ids=["no-budget", "no-cost", "zero-cost"],
)
def test_ram_capacity_is_unknown_when_either_input_is(budget, per_key):
    assert ram_capacity_keys(budget, per_key) is None


def test_the_effective_ceiling_is_the_nearest_one_and_names_itself():
    """Naming the binding ceiling is the point. A monitor that reports a number
    without saying what bounds it cannot tell the operator what to do — the
    defect #326 and #330 both describe."""
    c = effective_capacity(zone_keys=ZONE_KEYS, ram_keys=RAM_KEYS)
    assert c.keys == RAM_KEYS
    assert c.name == "ram"


def test_the_zone_binds_when_it_is_the_smaller_ceiling():
    c = effective_capacity(zone_keys=40_000_000, ram_keys=RAM_KEYS)
    assert c.keys == 40_000_000
    assert c.name == "zone"


def test_a_known_ceiling_wins_over_an_unknown_one():
    c = effective_capacity(zone_keys=ZONE_KEYS, ram_keys=None)
    assert c.keys == ZONE_KEYS
    assert c.name == "zone"


def test_with_no_ceiling_known_at_all_the_result_is_unknown():
    assert effective_capacity(None, None).keys is None


# --- projection -----------------------------------------------------------


def test_a_steady_climb_projects_a_sensible_number_of_days():
    history = [(0.0, 10_000_000.0), (10 * DAY, 11_000_000.0)]
    assert project_days_to(history, 12_000_000.0) == pytest.approx(10.0, rel=1e-6)


@pytest.mark.parametrize("history", [[], [(0.0, 1.0)]], ids=["empty", "single-point"])
def test_too_little_history_is_unknown_not_infinite(history):
    """Absence of a trend must never read as safe. Silence being mistaken for
    health is what let the July incident run for nine days."""
    assert project_days_to(history, 12_000_000.0) is None


def test_a_flat_trend_is_unknown_rather_than_never():
    history = [(0.0, 12_000_000.0), (10 * DAY, 12_000_000.0)]
    assert project_days_to(history, 13_000_000.0) is None


def test_a_shrinking_cache_does_not_project_a_comfortable_runway():
    """A falling object count means eviction is already happening. Extrapolating
    it gives a negative slope and a reassuring 'never' — the single most
    dangerous answer this function could return."""
    history = [(0.0, 12_000_000.0), (10 * DAY, 11_000_000.0)]
    assert project_days_to(history, 13_000_000.0) is None


def test_a_target_already_passed_projects_zero_days():
    history = [(0.0, 10_000_000.0), (10 * DAY, 14_000_000.0)]
    assert project_days_to(history, 13_000_000.0) == 0.0


def test_timestamps_that_do_not_advance_are_unknown():
    """A corrupt or duplicated history row must not divide by zero."""
    history = [(500.0, 10_000_000.0), (500.0, 11_000_000.0)]
    assert project_days_to(history, 12_000_000.0) is None


# --- verdict --------------------------------------------------------------


def test_a_healthy_cache_well_under_the_floor_is_up():
    history = [(0.0, 35_000_000.0), (30 * DAY, 35_600_000.0)]
    v = verdict(_sample(35_600_000), effective_capacity(ZONE_KEYS, RAM_KEYS), history)
    assert v.status == "up"


def test_crossing_the_floor_trips_down():
    history = [(0.0, 50_000_000.0), (30 * DAY, 50_100_000.0)]
    v = verdict(_sample(50_000_000), effective_capacity(ZONE_KEYS, RAM_KEYS), history)
    assert v.status == "down"
    assert "floor" in v.msg.lower()


def test_a_fast_climb_trips_down_long_before_the_floor():
    """The whole point of the projection. An Epic-style storm — prefills went
    4/day to 519/day over two days in July — must be caught weeks before the
    static floor would notice it."""
    history = [(0.0, 35_000_000.0), (10 * DAY, 40_000_000.0)]
    v = verdict(_sample(40_000_000), effective_capacity(ZONE_KEYS, RAM_KEYS), history)
    assert v.status == "down"


def test_an_unknown_trend_does_not_read_as_safe():
    """With no usable history the projection is unknown. The monitor must say so
    in words rather than quietly presenting a floor check as a full result."""
    v = verdict(_sample(35_600_000), effective_capacity(ZONE_KEYS, RAM_KEYS), [])
    assert "unknown" in v.msg.lower()


def test_a_failed_sample_pushes_down_rather_than_staying_silent():
    """Kuma treats silence as DOWN, but a probe that samples nothing and pushes
    nothing is indistinguishable from a healthy quiet night. It must speak."""
    failed = summarise_sample([], read_failures=256)
    v = verdict(failed, effective_capacity(ZONE_KEYS, RAM_KEYS), [])
    assert v.status == "down"
    assert "sample" in v.msg.lower()


def test_an_unknown_ceiling_pushes_down_rather_than_assuming_room():
    """If neither CACHE_INDEX_SIZE nor the RAM budget can be read, we do not know
    how much room is left. Not knowing is not the same as having room."""
    v = verdict(_sample(35_600_000), effective_capacity(None, None), [])
    assert v.status == "down"


def test_the_message_names_the_binding_ceiling():
    v = verdict(_sample(35_600_000), effective_capacity(ZONE_KEYS, RAM_KEYS), [])
    assert "ram" in v.msg.lower()


def test_the_message_carries_the_live_numbers():
    """A monitor that shows only a colour makes the operator go digging. The
    numbers that decide the verdict belong in the message that reports it."""
    v = verdict(_sample(35_600_000), effective_capacity(ZONE_KEYS, RAM_KEYS), [])
    assert "35.6M" in v.msg
    assert "54%" in v.msg
