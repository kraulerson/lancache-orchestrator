"""F11 — colorblind-safe terminal output (Intake §9: icon + text label, never
color alone). No ANSI color carries meaning; an icon glyph + the uppercased text
is the signal, so a colorblind operator reads the same state everyone else does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from collections.abc import Sequence

# Icon per state. Unmapped values fall back to a neutral dot (never KeyError).
_ICONS = {
    "ok": "✓",
    "up_to_date": "✓",
    "cached": "✓",
    "succeeded": "✓",
    "expired": "✗",
    "error": "✗",
    "failed": "✗",
    "validation_failed": "⚠",
    "blocked": "⛔",
    "pending_update": "↻",
    "downloading": "⬇",
    "running": "↻",
    "queued": "•",
    "cancelled": "⊘",
    "never": "○",
    "not_downloaded": "○",
    "unknown": "•",
}


def status_label(value: str) -> str:
    """``<icon> <UPPERCASE TEXT>`` — colorblind-safe. Stable for any string."""
    icon = _ICONS.get(value, "•")
    return f"{icon} {value.upper()}"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Fixed-width left-aligned table with a header rule. Cells coerced to str.

    Rows shorter than the header (sparse API records) render trailing blanks
    rather than raising ``IndexError`` mid-output.
    """
    ncols = len(headers)

    def _cell(row: Sequence[str], i: int) -> str:
        return str(row[i]) if i < len(row) else ""

    grid = [[str(h) for h in headers]] + [[_cell(r, i) for i in range(ncols)] for r in rows]
    widths = [max(len(row[i]) for row in grid) for i in range(ncols)]
    out = []
    for ri, row in enumerate(grid):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if ri == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def success(msg: str) -> None:
    click.echo(f"✓ {msg}")


def warn(msg: str) -> None:
    click.echo(f"⚠ {msg}")


def error(msg: str) -> None:
    click.echo(f"✗ {msg}", err=True)
