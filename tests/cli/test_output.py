"""F11: colorblind-safe output helpers (icon + text, never color alone)."""

from __future__ import annotations

import pytest

from orchestrator.cli.output import status_label, table

GAME_STATUSES = [
    "unknown",
    "not_downloaded",
    "up_to_date",
    "pending_update",
    "downloading",
    "validation_failed",
    "blocked",
    "failed",
]
AUTH_STATUSES = ["ok", "expired", "error", "never"]


@pytest.mark.parametrize("value", GAME_STATUSES + AUTH_STATUSES)
def test_status_label_has_icon_and_uppercase_text(value):
    label = status_label(value)
    assert value.upper() in label  # carries the text signal
    assert label[0].isalnum() is False  # leads with an icon glyph, not a bare word
    assert label != value  # not color-only / not the raw value


def test_status_label_unknown_value_is_safe():
    assert "WEIRD" in status_label("weird")  # never KeyErrors on an unmapped value


def test_table_aligns_and_includes_headers():
    out = table(["ID", "NAME"], [["1", "alpha"], ["22", "b"]])
    lines = out.splitlines()
    assert "ID" in lines[0] and "NAME" in lines[0]
    assert "alpha" in out and "22" in out
    # The ID column is padded to the widest cell ("22").
    assert lines[2].startswith("1 ")


def test_table_tolerates_ragged_rows():
    """A row shorter than the header (a sparse API record) must render blanks,
    not raise IndexError mid-output and crash the command."""
    out = table(["A", "B", "C"], [["1", "2"], ["x", "y", "z"]])
    lines = out.splitlines()
    assert "A" in lines[0] and "C" in lines[0]
    assert "1" in out and "z" in out  # both rows rendered, no IndexError
