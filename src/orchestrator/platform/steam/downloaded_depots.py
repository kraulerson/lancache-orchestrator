"""Clear SteamPrefill's "already downloaded" record for purged depots (#339).

SteamPrefill keeps ``Config/successfullyDownloadedDepots.json`` as
``{depot_id: [manifest_gids]}`` and skips any depot listed there. Purging a game
empties the cache underneath that record, so the next cron run sees "already
fetched" and the game never comes back. Verified in production on 2026-09-16/17:
a purged game was skipped by a cron run that completed OK, and removing its
depot key made the very next run re-fetch it.

Stdlib-only and importable by the agent (import-isolation, like
``selection_file``). NEVER touches credentials or the cache; only this one list.

**On the race.** SteamPrefill rewrites this file at the end of its own run, and
the agent does not hold the wrapper's prefill lock. The two orderings are:
ours-then-theirs, which loses our removal and leaves exactly today's bug; or
theirs-then-ours, which drops only the keys we intended. Neither corrupts the
file, because the write is atomic. Failing back to the status quo is acceptable;
destroying SteamPrefill's record of the whole library would not be, which is
also why an unparseable file is left strictly alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_log = structlog.get_logger(__name__)


def without_depots(state: dict[str, Any], depot_ids: Iterable[object]) -> dict[str, Any]:
    """A copy of ``state`` with ``depot_ids`` removed. Pure; never raises.

    The file keys depots as strings while the agent knows them as ints, so every
    id is coerced before lookup — a type mismatch here would silently remove
    nothing and quietly reinstate the defect.
    """
    drop = {str(d) for d in depot_ids}
    if not drop:
        return dict(state)
    return {k: v for k, v in state.items() if k not in drop}


async def clear_downloaded_depots(path: Path, depot_ids: Iterable[object]) -> int:
    """Remove ``depot_ids`` from SteamPrefill's downloaded record. Never raises.

    Returns the number of entries actually removed, so the caller can log
    whether the purge will really be undone.

    A missing file means SteamPrefill has never run here — nothing to clear. An
    unparseable file is left EXACTLY as found: rewriting something we could not
    read would discard the record for the entire library in order to re-fetch one
    game, which is far worse than the bug being fixed.
    """
    wanted = {str(d) for d in depot_ids}
    if not wanted:
        return 0
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _log.info("steam.downloaded_depots.absent", path=str(path))
        return 0
    except OSError as e:
        _log.warning("steam.downloaded_depots.unreadable", path=str(path), error=str(e)[:200])
        return 0

    try:
        state = json.loads(raw)
    except ValueError as e:
        _log.warning("steam.downloaded_depots.unparseable", path=str(path), error=str(e)[:200])
        return 0
    if not isinstance(state, dict):
        _log.warning("steam.downloaded_depots.unexpected_shape", path=str(path))
        return 0

    pruned = without_depots(state, wanted)
    removed = len(state) - len(pruned)
    if removed == 0:
        return 0

    # Atomic replace: SteamPrefill reads this on every run, and a half-written
    # file would make it re-download the whole library or fail outright.
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as fh:
            tmp_name = fh.name
            json.dump(pruned, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as e:
        _log.warning("steam.downloaded_depots.write_failed", path=str(path), error=str(e)[:200])
        return 0
    finally:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)

    _log.info("steam.downloaded_depots.cleared", path=str(path), removed=removed)
    return removed
