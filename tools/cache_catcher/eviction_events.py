"""Feature B — record each SENT eviction alert as one JSON line.

The orchestrator control plane reads this file (through the agent, which mounts
the shared volume read-only) and restarts the sweep pass at the eviction time, so
every game measured before the eviction is measured again.

Stdlib-only and separate from ``fanotify_guard.py`` for the same reason as
``delete_actor.py``: the guard loads ``libc.so.6`` at import, so its logic cannot
be tested off-box. Best-effort by contract: the alert's email and Kuma push matter
more than this file, so a write failure is logged and swallowed, never raised.

Line format (UTC, second precision):
    {"ts": "YYYY-MM-DDTHH:MM:SSZ", "deletes": int, "window_s": int, "actor": str}
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

EVENTS_PATH = "/events/eviction-events.jsonl"


def events_mount_status(path: str) -> str:
    """'writable', or a MISSING/READ-ONLY description — logged at guard startup so
    a missing cache-catcher-events mount is visible, not silent (final-review I4)."""
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        return f"MISSING: {directory} is not a directory (cache-catcher-events not mounted?)"
    if not os.access(directory, os.W_OK):
        return f"READ-ONLY: {directory} is not writable"
    return "writable"


def append_event(
    path: str,
    *,
    deletes: int,
    window_s: int,
    actor: str,
    log: Callable[[str], None],
) -> None:
    """Append one eviction event to ``path``. Never raises."""
    try:
        line = json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "deletes": int(deletes),
                "window_s": int(window_s),
                "actor": str(actor),
            },
            separators=(",", ":"),
        )
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception as e:  # best-effort by contract — see module docstring
        # A logger that itself fails must not turn this into a raise either.
        with contextlib.suppress(Exception):
            log(f"EVENT-WRITE FAILED ({type(e).__name__}): {path}")
