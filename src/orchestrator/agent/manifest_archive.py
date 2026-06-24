"""Durable manifest archive — copy SteamPrefill's transient .bin manifests into a
permanent, append-only store so validate can cover the whole prefilled library.

SteamPrefill only writes a manifest when an app has new content (and treats saved
manifests as temporary), so its live cache covers a shrinking subset of the
prefilled library. We snapshot every manifest we see into the archive; validate
reads the union (see manifest_locator). STDLIB ONLY — this module must not import
orchestrator.api / orchestrator.db (agent import-isolation guard,
tests/agent/test_import_isolation.py)."""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_log = structlog.get_logger(__name__)


def sync_manifests_to_archive(
    live_root: Path, archive_root: Path, *, settle_seconds: float = 10.0
) -> int:
    """Copy .bin files present in live/v1 but not archive/v1 (append-only).

    Preserves mtime (shutil.copy2), skips files written within ``settle_seconds``
    (may be mid-write — picked up next cycle), never deletes from the archive, and
    isolates per-file errors. Returns the number copied. A missing live dir or an
    unwritable archive is a no-op returning 0."""
    live_v1 = live_root / "v1"
    if not live_v1.is_dir():
        return 0
    archive_v1 = archive_root / "v1"
    try:
        archive_v1.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log.warning(
            "manifest_archive.mkdir_failed",
            archive=str(archive_v1),
            reason=f"{type(e).__name__}: {e}"[:200],
        )
        return 0
    existing = {p.name for p in archive_v1.glob("*.bin")}
    now = time.time()
    copied = 0
    for src in live_v1.glob("*.bin"):
        if src.name in existing:
            continue
        try:
            if now - src.stat().st_mtime < settle_seconds:
                continue
            shutil.copy2(src, archive_v1 / src.name)
            copied += 1
        except OSError as e:
            _log.warning(
                "manifest_archive.copy_failed",
                bin=src.name,
                reason=f"{type(e).__name__}: {e}"[:200],
            )
            continue
    if copied:
        _log.info("manifest_archive.synced", copied=copied, archive=str(archive_v1))
    return copied


async def manifest_archive_sync_loop(
    live_root: Path,
    archive_root: Path,
    interval_sec: int,
    *,
    settle_seconds: float = 10.0,
) -> None:
    """Run sync once immediately, then every ``interval_sec`` seconds, forever.

    The sync runs in a worker thread so the event loop is never blocked. Per-cycle
    errors are logged and swallowed (never kill the loop); CancelledError on
    shutdown propagates so the lifespan teardown can await the cancel."""
    while True:
        try:
            await asyncio.to_thread(
                sync_manifests_to_archive,
                live_root,
                archive_root,
                settle_seconds=settle_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let a bad cycle kill the loop
            _log.warning("manifest_archive.loop_error", reason=f"{type(e).__name__}: {e}"[:200])
        await asyncio.sleep(interval_sec)
