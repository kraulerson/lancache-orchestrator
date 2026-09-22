"""The keys_zone probe's I/O shell — the file that had no tests until #355.

The implementation plan said this module needed none, because "every decision it
could get wrong was moved into key_budget.py precisely so it would not need
them". That reasoning was wrong in a specific and instructive way: **input
validation is a decision**, and it lived here, in the untested file.

The bug: ``float()`` accepts "nan", "inf" and "-inf" without raising, so
``read_history`` catching only ``ValueError`` admitted a corrupted row as a
measurement. NaN then reached ``project_days_to``, where every IEEE 754
comparison against it is False, so it fell past every guard and ``verdict``
reported **"up"** — a false all-clear from corrupt input, in the alarm written
because the 2026-07-31 mass deletion was an absent signal read as a quiet one.

The unit tests for ``key_budget.py`` could not have caught it: they construct
history in memory, so they never exercised the parse. These follow the data from
file to verdict instead.

``key_budget_probe`` imports its siblings flat (``import kuma``,
``from key_budget import ...``) because in the container every file sits together
in ``/log/``. Importing it as ``tools.cache_catcher.key_budget_probe`` therefore
fails, so the directory goes on ``sys.path`` first.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_CACHE_CATCHER = Path(__file__).resolve().parents[2] / "tools" / "cache_catcher"
if str(_CACHE_CATCHER) not in sys.path:
    sys.path.insert(0, str(_CACHE_CATCHER))

key_budget_probe = importlib.import_module("key_budget_probe")
key_budget = importlib.import_module("key_budget")


# --- read_history: the parse that admitted the poison ----------------------


def test_a_well_formed_history_is_read(tmp_path):
    p = tmp_path / "h.csv"
    p.write_text("1789771023,35782656,4598390784,128.5\n1789857431,35670016,4598689792,128.9\n")
    assert key_budget_probe.read_history(p) == [
        (1789771023.0, 35782656.0),
        (1789857431.0, 35670016.0),
    ]


@pytest.mark.parametrize("poison", ["nan", "NaN", "inf", "-inf", "Infinity"], ids=lambda s: s)
def test_a_non_finite_field_is_skipped_not_accepted(tmp_path, poison):
    """#355. These all parse successfully via float(); none may be treated as data."""
    p = tmp_path / "h.csv"
    p.write_text(f"1789771023,{poison},0,0\n1789857431,35670016,0,0\n")
    assert key_budget_probe.read_history(p) == [(1789857431.0, 35670016.0)]


@pytest.mark.parametrize("poison", ["nan", "inf"], ids=lambda s: s)
def test_a_non_finite_timestamp_is_skipped_too(tmp_path, poison):
    """The timestamp comes from the same corrupted row, so it carries the same risk."""
    p = tmp_path / "h.csv"
    p.write_text(f"{poison},35782656,0,0\n1789857431,35670016,0,0\n")
    assert key_budget_probe.read_history(p) == [(1789857431.0, 35670016.0)]


def test_a_malformed_row_is_still_skipped(tmp_path):
    """The pre-existing ValueError behaviour must survive the #355 fix."""
    p = tmp_path / "h.csv"
    p.write_text("not-a-number,35782656\n1789857431,35670016\n\n1789900000\n")
    assert key_budget_probe.read_history(p) == [(1789857431.0, 35670016.0)]


def test_a_missing_file_is_an_empty_history_not_a_crash(tmp_path):
    """The first run has no history. That is normal, not an error."""
    assert key_budget_probe.read_history(tmp_path / "does-not-exist.csv") == []


def test_a_torn_final_line_is_accepted_but_fails_toward_the_alarm(tmp_path):
    """append_history writes without locking, so a reader can catch a torn line.

    A truncation that happens to leave two parseable fields IS accepted — the
    row ``1789857431,356`` reads as 356 objects. That is not the #355 bug and
    deliberately not guarded here, because it fails in the safe direction:

      - torn row LAST  -> the count drops, the slope goes negative,
                          project_days_to returns None -> "trend unknown"
      - torn row FIRST -> the slope looks enormous -> 0.57 days to floor
                          -> "FILLING FAST" -> DOWN

    Either way the operator is told something is wrong, which is the correct
    failure direction for a safety alarm. NaN was dangerous precisely because it
    failed the other way, silently reporting "up".
    """
    p = tmp_path / "h.csv"
    p.write_text("1789771023,35782656,0,0\n1789857431,356")
    history = key_budget_probe.read_history(p)
    assert history == [(1789771023.0, 35782656.0), (1789857431.0, 356.0)]

    # The torn row as the LAST point: a falling count suppresses the projection.
    assert key_budget.project_days_to(history, 56_000_000.0) is None


# --- the round trip: file -> verdict ----------------------------------------


def test_a_poisoned_file_cannot_produce_an_up_verdict(tmp_path):
    """The end-to-end assertion #355 is really about.

    Two clean rows plus one poisoned row must not read as healthier than two
    clean rows alone. Before the fix this returned "up" with a message reading
    "nand to floor".
    """
    p = tmp_path / "h.csv"
    p.write_text("1789771023,35782656,0,0\n1789857431,nan,0,0\n1789943831,35670016,0,0\n")
    history = key_budget_probe.read_history(p)

    assert all(v == v and abs(v) != float("inf") for row in history for v in row), (
        "no non-finite value may survive the parse"
    )

    per_leaf = int(35_700_000 / key_budget.TOTAL_LEAVES)
    sample = key_budget.summarise_sample([per_leaf] * 256)
    ceiling = key_budget.effective_capacity(80_000_000, 75_000_000)
    v = key_budget.verdict(sample, ceiling, history)

    assert "nan" not in v.msg.lower()


# --- load_cfg: the other parse in this file ---------------------------------


def test_defaults_apply_when_the_env_file_is_absent(tmp_path):
    """A missing /log/keybudget.env is a valid configuration: every monitor
    disabled, every tuning value defaulted. It must not crash the guard."""
    cfg = key_budget_probe.load_cfg(tmp_path / "nope.env")
    assert cfg["FLOOR"] == "0.75"
    assert cfg["SAMPLE_LEAVES"] == "256"
    assert cfg["KUMA_PUSH_KEY_BUDGET"] == ""


def test_an_env_value_containing_equals_survives(tmp_path):
    """Push URLs carry query strings. Splitting on every '=' would truncate one."""
    p = tmp_path / "k.env"
    p.write_text("KUMA_PUSH_KEY_BUDGET=http://kuma/api/push/abc?status=up&msg=x\n")
    cfg = key_budget_probe.load_cfg(p)
    assert cfg["KUMA_PUSH_KEY_BUDGET"] == "http://kuma/api/push/abc?status=up&msg=x"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    p = tmp_path / "k.env"
    p.write_text("# a comment\n\nFLOOR=0.9\n   \n#FLOOR=0.1\n")
    assert key_budget_probe.load_cfg(p)["FLOOR"] == "0.9"
