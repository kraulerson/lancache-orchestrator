"""Pure reconciliation of selectedAppsToPrefill.json (Piece 1 — Steam auto-prune)."""

from __future__ import annotations

from orchestrator.platform.steam.selection_file import reconcile_selection


def test_removes_excluded_app_ids():
    new, removed, restored = reconcile_selection([1, 2, 3], exclude_ids=[2], restore_ids=[])
    assert new == [1, 3]
    assert removed == 1
    assert restored == 0


def test_restore_wins_over_exclude():
    # A game both excluded and restored (allow overrides) stays in the list.
    new, removed, _ = reconcile_selection([1, 2], exclude_ids=[2], restore_ids=[2])
    assert new == [1, 2]
    assert removed == 0


def test_restore_readds_missing_app():
    # An allow'd app that was previously pruned is added back.
    new, _, restored = reconcile_selection([1], exclude_ids=[], restore_ids=[5])
    assert new == [1, 5]
    assert restored == 1


def test_idempotent_when_nothing_matches():
    new, removed, restored = reconcile_selection([1, 2, 3], exclude_ids=[9], restore_ids=[])
    assert new == [1, 2, 3]
    assert (removed, restored) == (0, 0)


def test_tolerates_string_ids_and_dedups():
    new, removed, _ = reconcile_selection(["1", "2", 2], exclude_ids=["2"], restore_ids=[])
    assert new == [1]
    assert removed == 1


def test_skips_non_integer_entries():
    new, _, _ = reconcile_selection([1, None, "x", 3], exclude_ids=[], restore_ids=[])
    assert new == [1, 3]
