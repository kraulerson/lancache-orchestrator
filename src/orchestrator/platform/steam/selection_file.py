"""Pure reconciliation of SteamPrefill's selectedAppsToPrefill.json (Piece 1).

The host SteamPrefill cron prefills every app in this file; to stop caching a
non-game the app must be REMOVED from it. The control plane classifies (it has
the Steam store types) and hands the agent the app_ids to remove ('exclude') and
to keep/re-add ('restore' = operator 'allow'). This module is the pure list
reconciliation — stdlib only, so the agent can import it (import-isolation).
NEVER touches credentials or the cache; only the app_id list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def as_int(value: object) -> int | None:
    """Parse an app_id (int or numeric str) to int, else None. Shared with the
    scheduler's prune actuation so both sides coerce ids identically."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _int_set(values: Iterable[object]) -> set[int]:
    out: set[int] = set()
    for v in values:
        i = as_int(v)
        if i is not None:
            out.add(i)
    return out


def reconcile_selection(
    current: Iterable[object],
    *,
    exclude_ids: Iterable[object],
    restore_ids: Iterable[object],
) -> tuple[list[int], int, int]:
    """Return (new_sorted_app_ids, removed_count, restored_count).

    Removes ``exclude_ids`` from ``current`` and ensures ``restore_ids`` are
    present. ``restore`` wins over ``exclude`` (an operator 'allow' keeps the app
    even if it's also classifier-excluded). Non-integer entries are dropped.
    """
    cur = _int_set(current)
    exclude = _int_set(exclude_ids)
    restore = _int_set(restore_ids)

    to_remove = cur & (exclude - restore)
    to_add = restore - cur
    new = (cur - to_remove) | restore
    return sorted(new), len(to_remove), len(to_add)
