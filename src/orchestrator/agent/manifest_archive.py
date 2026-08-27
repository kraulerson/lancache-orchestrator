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
import contextlib
import os
import shutil
import time
import uuid
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pathlib import Path

_log = structlog.get_logger(__name__)


def sync_manifests_to_archive(
    live_root: Path, archive_root: Path, *, settle_seconds: float = 10.0
) -> int:
    """Copy .bin files present in live/v1 but not archive/v1 (append-only).

    Copies with ``shutil.copyfile`` and then restores the source's mtime on the
    archived file — NOT ``copy2``, whose ``copystat`` would hand back a temp already
    carrying the source's stale mtime and let the orphan sweep unlink it mid-flight.
    Skips files written within ``settle_seconds`` (may be mid-write — picked up next
    cycle), never deletes from the archive, and isolates per-file errors. Returns the
    number copied. A missing live dir or an unwritable archive is a no-op returning 0."""
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
    # A ZERO-BYTE entry counts as absent, not as already-archived (#292). The old
    # copy wrote straight to the final name, so an interrupted one left a truncated
    # file that was then skipped by name forever — and an empty manifest validates
    # as a false green, which the 6-hourly sweep re-confirmed indefinitely. Treating
    # it as missing lets an install that already suffered that heal itself.
    existing = {p.name for p in archive_v1.glob("*.bin") if p.stat().st_size > 0}
    now = time.time()

    # Sweep orphaned temp files. Unique names fix the collision below but introduce
    # litter: one abandoned by a SIGKILL mid-copy is never touched again, where the
    # old fixed name at least self-overwrote on retry. Only OLD ones go — a recent
    # .partial may belong to a copy running right now, and removing it would
    # reintroduce the very race the unique names prevent.
    for orphan in archive_v1.glob("*.partial"):
        with contextlib.suppress(OSError):
            if now - orphan.stat().st_mtime > max(settle_seconds, 60.0):
                orphan.unlink()

    copied = 0
    for src in live_v1.glob("*.bin"):
        if src.name in existing:
            continue
        # A UNIQUE temp name per attempt. A fixed one is not enough: this function
        # has two concurrent callers — the background sync loop, and
        # _capture_prefill_manifests running it synchronously with settle_seconds=0
        # after a prefill. Sharing a temp name lets one truncate the other's file
        # mid-copy, and the rename then publishes a partial manifest under the final
        # name. That also defeats the zero-byte heal below, because the wreckage is
        # truncated-but-nonzero.
        tmp = archive_v1 / f".{src.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.partial"
        try:
            if now - src.stat().st_mtime < settle_seconds:
                continue
            # Copy to a temp name in the SAME directory, then rename. os.replace is
            # atomic within a filesystem, so the final name only ever refers to a
            # complete file — no reader can observe a half-written manifest, and a
            # crash mid-copy leaves the archive untouched rather than poisoned.
            src_stat = src.stat()

            # Two mtimes to satisfy, pulling in opposite directions.
            #
            # IN FLIGHT the temp must look NEW, or the orphan sweep above unlinks it
            # mid-copy. copyfile — NOT copy2 — because copystat is copy2's last act,
            # so copy2 hands back a temp already carrying the source's mtime, and a
            # manifest is only eligible to copy once it is older than the settle
            # window. Stamping it fresh afterwards merely moves the exposure into the
            # gap before the stamp; copyfile leaves the temp's mtime as its own write
            # time, fresh from the first byte and refreshed by every write during a
            # slow NFS copy.
            #
            # ARCHIVED it must keep the SOURCE's mtime. manifest_locator picks the
            # manifest to validate against with max(..., key=st_mtime) — newest wins
            # — so an archived file stamped "now" would outrank its siblings and
            # change which version validate compares against: the false-Partial bug
            # class (UAT-13 F2).
            #
            # RESIDUAL WINDOW, measured at ~9us: between the rename and the restore
            # the archived file carries the temp's fresh mtime, so a locator call
            # landing in that instant can pick it over a genuinely newer sibling.
            # Reaching it needs all of: this sync archiving a manifest that is NOT
            # its depot's newest (first backlog sync or a post-wipe heal — steady
            # state archives the newest anyway), a concurrent validate statting that
            # depot inside the window, and no prefilled_gids pin (#209 pins
            # agent-prefilled runs). Cost is one wrong verdict for one game,
            # corrected within 6h by the sweep. Stamping the temp with the source's
            # times just before the rename would close it but reopen the
            # sweep-eats-the-temp window instead — that failure is loud and benign
            # (ENOENT -> copy_failed -> retried) where this one is silent, so it is
            # arguably the better trade if this window ever proves reachable.
            #
            # NFS CAVEAT: the freshness argument above assumes one clock. On NFS the
            # temp's mtime comes from the server while the sweep compares against the
            # client's time.time(), so a server running >60s behind would make every
            # in-flight temp look stale and the archive would never grow (loudly —
            # copy_failed every cycle). Not the current deployment: the archive is a
            # local named volume written only by this agent.
            shutil.copyfile(src, tmp)
            dest = archive_v1 / src.name
            os.replace(tmp, dest)
            try:
                os.utime(dest, (src_stat.st_atime, src_stat.st_mtime))
            except OSError as e:
                # NOT suppressed. If this fails the archived manifest keeps the fresh
                # mtime permanently and outranks every sibling for selection until a
                # newer one lands — silently. The file's contents are correct, so
                # this is a warning rather than a failure, but it must be visible.
                _log.warning(
                    "manifest_archive.mtime_restore_failed",
                    bin=src.name,
                    reason=f"{type(e).__name__}: {e}"[:200],
                )
            copied += 1
        except OSError as e:
            # Remove the stub so the next sync is free to retry.
            with contextlib.suppress(OSError):
                tmp.unlink()
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
