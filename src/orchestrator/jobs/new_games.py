"""New - Not Cached: check-set and overdue-set selection.

A game is "new" for ``window_days`` after ``games.first_seen_at`` (UTC
``YYYY-MM-DDTHH:MM:SSZ``, stamped by library sync). Window maths run in SQL
against ``strftime(...,'now', modifier)`` so the comparison is UTC string order.
Rows with NULL ``first_seen_at`` are never selected. Read-only: nothing here
writes ``games.status`` (single-writer rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from orchestrator.clients import heartbeat

if TYPE_CHECKING:
    from orchestrator.db.pool import Pool

# Derived, not duplicated: heartbeat.push keeps only the LAST MSG_MAX_CHARS of a
# message, so anything longer would lose the count and the first titles.
_KUMA_MSG_MAX = heartbeat.MSG_MAX_CHARS

# Eligibility shared by both statements: owned, steam/epic, first_seen_at NOT NULL,
# not blocked, not explicitly excluded (mode 'allow' does NOT exclude). One constant so
# the two statements cannot drift. Static text only; values are always bound.
_ELIGIBLE = """
    g.owned = 1
    AND g.platform IN ('steam','epic')
    AND g.first_seen_at IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM block_list b
                    WHERE b.platform = g.platform AND b.app_id = g.app_id)
    AND NOT EXISTS (SELECT 1 FROM prefill_exclusions x
                    WHERE x.platform = g.platform AND x.app_id = g.app_id
                      AND x.mode = 'exclude')
"""

_CHECK_SQL = (
    "SELECT g.id FROM games g WHERE"  # noqa: S608 - joins module constants only; values bound
    + _ELIGIBLE
    + """  AND g.first_seen_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-' || ? || ' days')
  AND g.status IN ('unknown','not_downloaded','validation_failed')
ORDER BY g.first_seen_at, g.id
"""
)

# pending_update counts as cached. Branch (1) wins over (2) for a game matching both.
_OVERDUE_SQL = (
    """
SELECT g.id, g.title,
       CASE WHEN g.first_seen_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', '-' || ? || ' days')
                 AND g.status NOT IN ('up_to_date','pending_update')
            THEN 'not cached' ELSE 'unmeasured' END AS reason
FROM games g
WHERE"""
    + _ELIGIBLE
    + """  AND (
        (g.first_seen_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', '-' || ? || ' days')
         AND g.status NOT IN ('up_to_date','pending_update'))
     OR (g.status = 'unknown'
         AND g.first_seen_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', '-' || ? || ' hours'))
  )
ORDER BY g.first_seen_at, g.id
"""
)


@dataclass(frozen=True)
class OverdueGame:
    game_id: int
    title: str
    reason: Literal["not cached", "unmeasured"]


async def select_check_set(pool: Pool, *, window_days: int) -> list[int]:
    rows = await pool.read_all(_CHECK_SQL, (window_days,))
    return [int(r["id"]) for r in rows]


async def select_overdue(pool: Pool, *, window_days: int, unknown_hours: int) -> list[OverdueGame]:
    rows = await pool.read_all(_OVERDUE_SQL, (window_days, window_days, unknown_hours))
    return [
        OverdueGame(
            game_id=int(r["id"]),
            title=str(r["title"]),
            reason="not cached" if r["reason"] == "not cached" else "unmeasured",
        )
        for r in rows
    ]


def overdue_message(overdue: list[OverdueGame]) -> str:
    """Kuma push message, at most ``_KUMA_MSG_MAX`` characters."""
    if not overdue:
        return "no new games overdue"
    prefix = f"overdue {len(overdue)}: "
    parts: list[str] = []
    for i, g in enumerate(overdue):
        part = f"{g.title} ({g.reason})"
        candidate = prefix + ", ".join([*parts, part])
        remaining = len(overdue) - (i + 1)
        tail = f"…(+{remaining} more)" if remaining else ""
        if len(candidate) + len(tail) > _KUMA_MSG_MAX:
            break
        parts.append(part)
    shown = len(parts)
    if shown == len(overdue):
        return prefix + ", ".join(parts)
    tail = f"…(+{len(overdue) - shown} more)"
    if not parts:
        # Even the first entry does not fit: truncate it, keep the count honest.
        tail = f"…(+{len(overdue) - 1} more)"
        room = _KUMA_MSG_MAX - len(prefix) - len(tail)
        first = f"{overdue[0].title} ({overdue[0].reason})"
        return prefix + first[: max(room, 0)] + tail
    return prefix + ", ".join(parts) + tail
