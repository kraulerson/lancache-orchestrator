"""A half-written archive entry must not become permanent.

UAT-14 #292, second half. ``sync_manifests_to_archive`` copied straight to the final
archive filename and then skipped by filename forever:

    existing = {p.name for p in archive_v1.glob("*.bin")}
    ...
    if src.name in existing: continue
    shutil.copy2(src, archive_v1 / src.name)

So an interrupted copy — a full disk, a crash, an NFS hiccup mid-write — leaves a
truncated or zero-byte file under the real name, and it is never replaced even with a
complete source sitting in the live cache. Combined with the validator treating a
zero-chunk manifest as ``cached``, that made a false green self-perpetuating: the
sweep re-confirmed it every six hours.

Two properties fix it. The write is atomic, so a partial file never appears under the
final name at all; and a zero-byte entry already in the archive is treated as absent,
so an install that already suffered the bug heals itself on the next sync.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from orchestrator.agent.manifest_archive import sync_manifests_to_archive

if TYPE_CHECKING:
    from pathlib import Path


def _live_manifest(live: Path, name: str, content: bytes, *, age_seconds: float = 60.0) -> Path:
    v1 = live / "v1"
    v1.mkdir(parents=True, exist_ok=True)
    f = v1 / name
    f.write_bytes(content)
    # Backdate it past the settle window, or the sync skips it as mid-write.
    old = time.time() - age_seconds
    os.utime(f, (old, old))
    return f


def test_a_zero_byte_archive_entry_is_replaced(tmp_path: Path) -> None:
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"a complete manifest")

    archive_v1 = archive / "v1"
    archive_v1.mkdir(parents=True)
    (archive_v1 / "app.bin").write_bytes(b"")  # the wreckage of an interrupted copy

    copied = sync_manifests_to_archive(live, archive)

    assert copied == 1, "a zero-byte archive entry must not count as already present"
    assert (archive_v1 / "app.bin").read_bytes() == b"a complete manifest", (
        "the truncated entry must be replaced by the complete source, or the false "
        "green it causes is permanent"
    )


def test_a_complete_archive_entry_is_left_alone(tmp_path: Path) -> None:
    """The archive stays append-only for entries that are actually intact."""
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"newer content")

    archive_v1 = archive / "v1"
    archive_v1.mkdir(parents=True)
    (archive_v1 / "app.bin").write_bytes(b"original archived content")

    copied = sync_manifests_to_archive(live, archive)

    assert copied == 0
    assert (archive_v1 / "app.bin").read_bytes() == b"original archived content", (
        "an intact archive entry is the durable record and must never be overwritten"
    )


def test_no_temporary_file_survives_a_successful_sync(tmp_path: Path) -> None:
    """The archive holds the finished file and nothing else.

    HONEST SCOPE: this does not prove atomicity — it passes against the old
    straight-to-final-name copy too. Atomicity comes from os.replace being atomic
    within a filesystem, which is a property of the construction rather than
    something a test can observe without racing the write. What this DOES guard is
    the failure mode a temp-and-rename implementation introduces: leftover .partial
    files accumulating in the archive.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    _live_manifest(live, "app.bin", b"x" * 4096)

    sync_manifests_to_archive(live, archive)

    entries = sorted(p.name for p in (archive / "v1").iterdir())
    assert entries == ["app.bin"], f"temporary files must not survive the sync: {entries}"
    assert (archive / "v1" / "app.bin").read_bytes() == b"x" * 4096


def test_a_failed_copy_leaves_nothing_behind(tmp_path: Path) -> None:
    """A failed copy leaves the archive unchanged, with no stub blocking a retry.

    HONEST SCOPE: with an unreadable source the copy fails before it creates
    anything, so this passes against the old implementation as well. It is kept as a
    guard on the new one — the temp file must be cleaned up on any error path, or a
    disk-full mid-copy would leave .partial files behind forever.
    """
    live, archive = tmp_path / "live", tmp_path / "archive"
    src = _live_manifest(live, "app.bin", b"content")
    (archive / "v1").mkdir(parents=True)

    # Make the source unreadable so the copy fails partway through the attempt.
    src.chmod(0o000)
    try:
        copied = sync_manifests_to_archive(live, archive)
    finally:
        src.chmod(0o644)

    assert copied == 0
    assert list((archive / "v1").iterdir()) == [], (
        "a failed copy must not leave a stub — the next sync has to be free to retry"
    )
